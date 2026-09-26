# classic Hyundai 0x238 객체 디코더의 주소 구조와 운동학 값을 검증하는 테스트
import pytest

from opendbc.car.hyundai.radar_classic_238 import (
  CLASSIC_238_END_ADDR,
  CLASSIC_238_START_ADDR,
  Classic238Object,
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
