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


def classic_238_address_role(address: int) -> tuple[int, int]:
  if not CLASSIC_238_START_ADDR <= address <= CLASSIC_238_END_ADDR:
    raise ValueError(f"address 0x{address:x} is outside classic 0x238 object range")

  offset = address - CLASSIC_238_START_ADDR
  return offset // CLASSIC_238_FRAMES_PER_SLOT, offset % CLASSIC_238_FRAMES_PER_SLOT
