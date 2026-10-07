import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.backfill_rlog import Backfill, capture_id, params_settings, segment_number


class FakeEvent:
  def __init__(self, kind, mono):
    self.kind, self.logMonoTime = kind, mono

  def which(self):
    return self.kind


class BackfillTest(unittest.TestCase):
  def test_settings_come_from_the_logged_params_snapshot(self):
    entries = {"CustomSteerDeltaUp": b"4", "StoppingAccel": b"-20\n", "Broken": b"x", "Other": b"1"}
    self.assertEqual(params_settings(entries, ("CustomSteerDeltaUp", "StoppingAccel", "Broken", "Missing")),
                     {"CustomSteerDeltaUp": 4.0, "StoppingAccel": -20.0})

  def test_segment_order_and_stable_capture_ids(self):
    self.assertEqual(segment_number(Path("00000044--71ba6fcdb0--12")), 12)
    self.assertEqual(capture_id("r", 5), capture_id("r", 5))
    self.assertNotEqual(capture_id("r", 5), capture_id("r", 6))

  def test_transport_stream_video_is_cut_at_thirty_seconds(self):
    from tools.c4_diagnostics.backfill_rlog import ts_h264

    def pts_bytes(pts):
      return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF,
                    ((pts << 1) & 0xFE) | 1])

    def packet(pid, payload, start):
      header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
      return (header + payload).ljust(188, b"\xff")

    stream = b""
    for index in range(40):  # one frame per second
      nal = b"\x00\x00\x00\x01" + bytes([0x65, index])
      pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts_bytes(index * 90000) + nal
      stream += packet(0x100, pes, True)
      stream += packet(0x101, b"audio", True)
    data, frames = ts_h264(stream, 30.0)
    self.assertEqual(frames, 30)
    self.assertTrue(data.startswith(b"\x00\x00\x00\x01\x65\x00"))
    self.assertNotIn(b"audio", data)

  def test_periodic_bundles_every_ten_minutes_and_reruns_skip_existing(self):
    with tempfile.TemporaryDirectory() as directory:
      spool = Path(directory)

      def run():
        backfill = Backfill(spool, "route", (), lambda latest: {}, lambda latest: {})
        backfill.wall_offset_ns = 1_791_000_000 * 10**9
        opened = []
        for minute in range(25):
          start = minute * 60 * 10**9
          backfill.start_segment(spool / f"route--{minute}", [FakeEvent("can", start)])
          if backfill.writer is not None and backfill.writer.started_at not in opened:
            opened.append(backfill.writer.started_at)
            backfill.writer.append(start, 0x238, 1, bytes(8))
        backfill.finish()
        return backfill

      first = run()
      self.assertEqual(len(first.made), 3)  # minutes 0, 10, 20
      second = run()
      self.assertEqual((len(second.made), second.skipped), (0, 3))


if __name__ == "__main__":
  unittest.main()
