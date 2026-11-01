package com.carrot.aaprobe

import androidx.car.app.navigation.model.Maneuver

/**
 * TMAP-style turn codes (Carrot Navi v2 guidance turn_type, also used for Naver via CarrotNM)
 * to Android Auto maneuvers. Groups follow selfdrive/carrot/carrot_serv.py nav_type_mapping.
 */
object TurnMap {
  data class Turn(val type: Int, val label: String, val exitAngle: Int = 0)

  private val SLIGHT_LEFT_RAMP = setOf(102, 105, 112, 115)
  private val SLIGHT_RIGHT_RAMP = setOf(101, 104, 111, 114)
  private val KEEP_LEFT = setOf(7, 17, 44, 75, 76, 118)
  private val KEEP_RIGHT = setOf(6, 43, 73, 74, 117, 123, 124)
  /** Rotary codes → counter-clockwise exit angle (right-hand traffic). */
  private val ROTARY = mapOf(
    131 to 45, 132 to 45, 133 to 90, 134 to 135, 135 to 135, 142 to 180,
    136 to 225, 137 to 225, 138 to 225, 139 to 270, 140 to 315, 141 to 315,
  )

  fun of(code: Int): Turn = when {
    code == 11 -> Turn(Maneuver.TYPE_STRAIGHT, "직진")
    code == 12 -> Turn(Maneuver.TYPE_TURN_NORMAL_LEFT, "좌회전")
    code == 13 -> Turn(Maneuver.TYPE_TURN_NORMAL_RIGHT, "우회전")
    code == 14 -> Turn(Maneuver.TYPE_U_TURN_LEFT, "유턴")
    code == 16 -> Turn(Maneuver.TYPE_TURN_SHARP_LEFT, "급좌회전")
    code == 19 -> Turn(Maneuver.TYPE_TURN_SHARP_RIGHT, "급우회전")
    code == 18 -> Turn(Maneuver.TYPE_TURN_SLIGHT_RIGHT, "오른쪽 방향")
    code == 1000 -> Turn(Maneuver.TYPE_TURN_SLIGHT_LEFT, "왼쪽 방향")
    code == 1001 -> Turn(Maneuver.TYPE_TURN_SLIGHT_RIGHT, "오른쪽 방향")
    code == 1002 -> Turn(Maneuver.TYPE_FORK_LEFT, "왼쪽 분기")
    code == 1003 -> Turn(Maneuver.TYPE_FORK_RIGHT, "오른쪽 분기")
    code == 1006 -> Turn(Maneuver.TYPE_OFF_RAMP_NORMAL_LEFT, "왼쪽 출구")
    code == 1007 -> Turn(Maneuver.TYPE_OFF_RAMP_NORMAL_RIGHT, "오른쪽 출구")
    code in SLIGHT_LEFT_RAMP -> Turn(Maneuver.TYPE_OFF_RAMP_SLIGHT_LEFT, "왼쪽 출구")
    code in SLIGHT_RIGHT_RAMP -> Turn(Maneuver.TYPE_OFF_RAMP_SLIGHT_RIGHT, "오른쪽 출구")
    code in KEEP_LEFT -> Turn(Maneuver.TYPE_KEEP_LEFT, "왼쪽 유지")
    code in KEEP_RIGHT -> Turn(Maneuver.TYPE_KEEP_RIGHT, "오른쪽 유지")
    code in ROTARY -> Turn(Maneuver.TYPE_ROUNDABOUT_ENTER_AND_EXIT_CCW_WITH_ANGLE, "회전교차로", ROTARY.getValue(code))
    code == 200 -> Turn(Maneuver.TYPE_DEPART, "출발")
    code == 201 -> Turn(Maneuver.TYPE_DESTINATION, "목적지")
    else -> Turn(Maneuver.TYPE_UNKNOWN, "안내($code)")
  }
}
