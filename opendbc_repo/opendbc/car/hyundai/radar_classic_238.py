# classic Hyundai 0x238 객체 목록의 검증된 운동학 필드를 해석하는 디코더
from dataclasses import dataclass


CLASSIC_238_START_ADDR = 0x238
CLASSIC_238_SLOT_COUNT = 10
CLASSIC_238_FRAMES_PER_SLOT = 3
CLASSIC_238_END_ADDR = CLASSIC_238_START_ADDR + CLASSIC_238_SLOT_COUNT * CLASSIC_238_FRAMES_PER_SLOT - 1


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


def classic_238_address_role(address: int) -> tuple[int, int]:
  if not CLASSIC_238_START_ADDR <= address <= CLASSIC_238_END_ADDR:
    raise ValueError(f"address 0x{address:x} is outside classic 0x238 object range")

  offset = address - CLASSIC_238_START_ADDR
  return offset // CLASSIC_238_FRAMES_PER_SLOT, offset % CLASSIC_238_FRAMES_PER_SLOT
