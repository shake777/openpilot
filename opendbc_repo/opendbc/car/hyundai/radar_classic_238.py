# classic Hyundai 0x238 객체 목록의 검증된 운동학 필드를 해석하는 디코더
from dataclasses import dataclass


CLASSIC_238_START_ADDR = 0x238
CLASSIC_238_SLOT_COUNT = 10
CLASSIC_238_FRAMES_PER_SLOT = 3
CLASSIC_238_END_ADDR = CLASSIC_238_START_ADDR + CLASSIC_238_SLOT_COUNT * CLASSIC_238_FRAMES_PER_SLOT - 1
CLASSIC_238_MAX_TRIPLET_AGE_NS = 150_000_000


@dataclass(frozen=True)
class Classic238Object:
  status: int
  d_rel: float
  y_rel: float
  v_lead: float
  raw: int

  @classmethod
  def from_first_frame(cls, payload: bytes) -> "Classic238Object":
    if len(payload) != 8:
      raise ValueError(f"classic 0x238 object frame must be 8 bytes, got {len(payload)}")

    raw = int.from_bytes(payload, "big")
    status = (raw >> 48) & 0x7
    d_rel = ((raw >> 51) & 0x1FFF) * 0.05
    y_sensor = ((raw >> 36) & 0xFFF) * 0.05 - 102.4
    v_lead = ((raw >> 20) & 0xFFF) * 0.05 - 100.0
    return cls(status=status, d_rel=d_rel, y_rel=-y_sensor, v_lead=v_lead, raw=raw)


@dataclass(frozen=True)
class Classic238Triplet:
  obj: Classic238Object
  rolling_counters: tuple[int, int, int]
  object_sequence: int
  second_frame: bytes
  third_frame: bytes

  @property
  def counter_consistent(self) -> bool:
    return len(set(self.rolling_counters)) == 1

  @classmethod
  def from_frames(cls, first: bytes, second: bytes, third: bytes) -> "Classic238Triplet":
    if len(second) != 8 or len(third) != 8:
      raise ValueError("classic 0x238 follow-up frames must be 8 bytes")

    obj = Classic238Object.from_first_frame(first)
    # The same two-bit rolling counter is present in all three frames. It was
    # identical for every complete triplet in the K7, K5 and Sonata captures.
    first_counter = (obj.raw >> 18) & 0x3
    second_counter = (second[0] >> 6) & 0x3
    third_counter = (third[0] >> 6) & 0x3
    # This byte advances while a slot is active and normally freezes while it
    # is empty. It does not reset to a fixed value when a new object appears,
    # so expose it as a sequence value rather than claiming it is an object ID.
    object_sequence = third[3]
    return cls(obj=obj, rolling_counters=(first_counter, second_counter, third_counter),
               object_sequence=object_sequence, second_frame=second, third_frame=third)


@dataclass(frozen=True)
class Classic238RadarKinematics:
  d_rel: float
  y_rel: float
  v_rel: float
  v_lead: float
  a_rel: float
  yv_rel: float

  @classmethod
  def from_triplet(cls, triplet: Classic238Triplet, v_ego: float) -> "Classic238RadarKinematics":
    return cls(d_rel=triplet.obj.d_rel, y_rel=triplet.obj.y_rel,
               v_rel=triplet.obj.v_lead - v_ego, v_lead=triplet.obj.v_lead,
               a_rel=float("nan"), yv_rel=0.0)


class Classic238Assembler:
  def __init__(self, max_age_ns: int = CLASSIC_238_MAX_TRIPLET_AGE_NS):
    self.max_age_ns = max_age_ns
    self.frames: list[list[tuple[int, bytes] | None]] = [
      [None] * CLASSIC_238_FRAMES_PER_SLOT for _ in range(CLASSIC_238_SLOT_COUNT)
    ]
    self.last_emitted: list[tuple[int, int, int] | None] = [None] * CLASSIC_238_SLOT_COUNT

  def update(self, mono_time_ns: int, address: int, payload: bytes) -> tuple[int, Classic238Triplet] | None:
    slot, role = classic_238_address_role(address)
    if len(payload) != 8:
      raise ValueError(f"classic 0x238 frame must be 8 bytes, got {len(payload)}")
    self.frames[slot][role] = (mono_time_ns, payload)
    complete = self.frames[slot]
    if any(frame is None for frame in complete):
      return None
    typed = [frame for frame in complete if frame is not None]
    times = tuple(frame[0] for frame in typed)
    if max(times) - min(times) > self.max_age_ns or mono_time_ns - max(times) > self.max_age_ns:
      return None
    triplet = Classic238Triplet.from_frames(*(frame[1] for frame in typed))
    if not triplet.counter_consistent or self.last_emitted[slot] == times:
      return None
    self.last_emitted[slot] = times
    return slot, triplet


def classic_238_address_role(address: int) -> tuple[int, int]:
  if not CLASSIC_238_START_ADDR <= address <= CLASSIC_238_END_ADDR:
    raise ValueError(f"address 0x{address:x} is outside classic 0x238 object range")

  offset = address - CLASSIC_238_START_ADDR
  return offset // CLASSIC_238_FRAMES_PER_SLOT, offset % CLASSIC_238_FRAMES_PER_SLOT
