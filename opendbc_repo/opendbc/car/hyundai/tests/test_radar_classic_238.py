# classic Hyundai 0x238 객체 디코더의 주소 구조와 운동학 값을 검증하는 테스트
import pytest

from opendbc.car.hyundai.radar_classic_238 import (
  CLASSIC_238_END_ADDR,
  CLASSIC_238_START_ADDR,
  Classic238AssociationTracker,
  Classic238Assembler,
  Classic238DisplayTracker,
  Classic238Object,
  Classic238RadarKinematics,
  Classic238TrackObservation,
  Classic238Triplet,
  classic_238_address_role,
)


class TestClassic238ObjectDecoder:
  def test_k7_active_object_kinematics(self):
    obj = Classic238Object.from_first_frame(bytes.fromhex("0c797fdb8fe1f204"))

    assert obj.status == 1
    assert obj.d_rel == pytest.approx(19.95)
    assert obj.y_rel == pytest.approx(0.15)
    assert obj.v_lead == pytest.approx(15.10)

  @pytest.mark.parametrize(
    "address, expected",
    (
      (0x238, (0, 0)),
      (0x239, (0, 1)),
      (0x23A, (0, 2)),
      (0x23B, (1, 0)),
      (0x253, (9, 0)),
      (0x255, (9, 2)),
    ),
  )
  def test_three_frames_map_to_ten_slots(self, address, expected):
    assert classic_238_address_role(address) == expected

  @pytest.mark.parametrize("address", (CLASSIC_238_START_ADDR - 1, CLASSIC_238_END_ADDR + 1))
  def test_address_outside_group_is_rejected(self, address):
    with pytest.raises(ValueError):
      classic_238_address_role(address)

  def test_non_eight_byte_frame_is_rejected(self):
    with pytest.raises(ValueError):
      Classic238Object.from_first_frame(bytes(7))

  def test_triplet_counters_and_object_sequence(self):
    triplet = Classic238Triplet.from_frames(
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("0f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )

    assert triplet.counter_consistent
    assert triplet.rolling_counters == (0, 0, 0)
    assert triplet.object_sequence == 0xE8
    assert triplet.obj.d_rel == pytest.approx(19.95)

  def test_triplet_counter_mismatch_is_visible(self):
    triplet = Classic238Triplet.from_frames(
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("4f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )

    assert not triplet.counter_consistent
    assert triplet.rolling_counters == (0, 1, 0)

  def test_live_assembler_emits_one_complete_consistent_triplet(self):
    assembler = Classic238Assembler()
    frames = (
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("0f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )
    assert assembler.update(1_000_000_000, 0x238, frames[0]) is None
    assert assembler.update(1_010_000_000, 0x239, frames[1]) is None
    slot, triplet = assembler.update(1_020_000_000, 0x23A, frames[2])
    assert slot == 0
    assert triplet.object_sequence == 0xE8
    assert assembler.update(1_020_000_000, 0x23A, frames[2]) is None

  def test_live_assembler_rejects_counter_mismatch_and_stale_triplet(self):
    mismatch = Classic238Assembler()
    assert mismatch.update(1_000_000_000, 0x238, bytes.fromhex("0c797fdb8fe1f204")) is None
    assert mismatch.update(1_010_000_000, 0x239, bytes.fromhex("4f9d774945c80002")) is None
    assert mismatch.update(1_020_000_000, 0x23A, bytes.fromhex("085c40e8ffe084ec")) is None

    stale = Classic238Assembler(max_age_ns=10)
    assert stale.update(100, 0x238, bytes.fromhex("0c797fdb8fe1f204")) is None
    assert stale.update(105, 0x239, bytes.fromhex("0f9d774945c80002")) is None
    assert stale.update(120, 0x23A, bytes.fromhex("085c40e8ffe084ec")) is None

  def test_radar_kinematics_are_ready_for_radar_point_mapping(self):
    triplet = Classic238Triplet.from_frames(
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("0f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )
    point = Classic238RadarKinematics.from_triplet(triplet, v_ego=20.0)
    assert point.d_rel == pytest.approx(19.95)
    assert point.y_rel == pytest.approx(0.15)
    assert point.v_lead == pytest.approx(15.10)
    assert point.v_rel == pytest.approx(-4.90)
    assert point.yv_rel == 0.0

  def test_display_tracker_keeps_active_track_and_expires_it(self):
    tracker = Classic238DisplayTracker(max_age_ns=30_000_000)
    frames = (
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("0f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )
    for offset, payload in enumerate(frames):
      tracker.update(1_000_000_000 + offset * 10_000_000, 0x238 + offset, payload, v_ego=20.0)
    tracker.finish_scan(1_020_000_000)

    tracks = tracker.current(1_020_000_000)
    assert len(tracks) == 1
    assert tracks[0].slot == 0
    assert tracks[0].track_id == 0
    assert tracks[0].status == 1
    assert tracks[0].object_sequence == 0xE8
    assert tracks[0].kinematics.v_rel == pytest.approx(-4.90)
    assert tracker.current(1_050_000_001) == []

  def test_display_tracker_removes_empty_slot(self):
    tracker = Classic238DisplayTracker()
    active = (
      bytes.fromhex("0c797fdb8fe1f204"),
      bytes.fromhex("0f9d774945c80002"),
      bytes.fromhex("085c40e8ffe084ec"),
    )
    for offset, payload in enumerate(active):
      tracker.update(1_000_000_000 + offset, 0x238 + offset, payload, v_ego=20.0)
    tracker.finish_scan(1_000_000_002)
    assert len(tracker.current(1_000_000_002)) == 1

    empty = (
      bytes.fromhex("0000000000000000"),
      bytes.fromhex("0000000000000000"),
      bytes.fromhex("0000000100000000"),
    )
    for offset, payload in enumerate(empty):
      tracker.update(1_010_000_000 + offset, 0x238 + offset, payload, v_ego=20.0)
    tracker.finish_scan(1_010_000_002)
    assert len(tracker.current(1_010_000_002)) == 1
    assert tracker.current(1_200_000_003) == []

  def test_association_keeps_id_across_slot_change_and_short_loss(self):
    tracker = Classic238AssociationTracker(max_age_ns=150_000_000)

    def observation(slot, d_rel, y_rel, v_lead):
      return Classic238TrackObservation(
        slot=slot, status=2, object_sequence=0,
        kinematics=Classic238RadarKinematics(
          d_rel=d_rel, y_rel=y_rel, v_rel=-1.0, v_lead=v_lead,
          a_rel=float("nan"), yv_rel=0.0,
        ),
      )

    first = tracker.update_scan(1_000_000_000, [observation(3, 30.0, -1.0, 20.0)])
    assert first[0].track_id == 0
    assert tracker.update_scan(1_050_000_000, [])[0].track_id == 0
    moved = tracker.update_scan(1_100_000_000, [observation(2, 29.9, -1.1, 20.2)])
    assert len(moved) == 1
    assert moved[0].track_id == 0
    assert moved[0].slot == 2

  def test_association_does_not_merge_distant_objects(self):
    tracker = Classic238AssociationTracker()
    first = Classic238TrackObservation(
      slot=0, status=2, object_sequence=0,
      kinematics=Classic238RadarKinematics(10.0, 0.0, 0.0, 10.0, float("nan"), 0.0),
    )
    distant = Classic238TrackObservation(
      slot=0, status=2, object_sequence=1,
      kinematics=Classic238RadarKinematics(30.0, 0.0, 0.0, 10.0, float("nan"), 0.0),
    )
    tracker.update_scan(1_000_000_000, [first])
    tracks = tracker.update_scan(1_050_000_000, [distant])
    assert {track.track_id for track in tracks} == {0, 1}
