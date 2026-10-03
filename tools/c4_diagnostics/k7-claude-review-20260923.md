# K7 종방향 및 SCC 읽기 문서 검토 결과

2026-09-23. 두 첨부 문서는 작업 제안으로 읽었으며 원본 DBC·실행 코드를 직접 대조했다.

## 종방향 가속 미달

`longitudinal_planner.py`에서 ACC 모드 가속 상한은 실제로 `carrot.get_carrot_accel(v_ego)`를 사용한다. 이 함수는 `CruiseMaxVals0`~`6`을 속도에 따라 보간하고 주행 모드 계수를 곱한다. 따라서 조사할 합리적인 후보이지만 증상의 원인으로 확정되지는 않았다. 첨부 문서가 인용한 `hyundaicanfd.py`의 `AccelLimitBandUpper=1.26` 주석은 K7의 non-CANFD 경로에 직접 적용되지 않는다. K7의 가속 미달을 그것으로 설명할 수 없다. 설정 속도 가공, 턴 제한, 선행차 판단, 차량의 명령 수용 여부도 같은 시간대 주행 로그에서 구분해야 한다. 제어 파라미터 변경은 하지 않았다.

차량 로그의 첫 분석 기준은 `carState.vEgo/vCruise/aEgo`, `carControl.longActive/actuators.accel`, `longitudinalPlan`, `radarState.leadOne`, 해당 시점의 설정 스냅샷이다. 로그의 설정 속도와 실제 속도가 다른 구간을 골라 계획 가속과 송신 가속을 시간 정렬해야 한다. 현재 이 PC의 `\\DS1821P\openpilot\routes`와 `W:`가 연결되지 않아 실차 구간을 읽지 못했다.

## SCC 읽기

첨부 문서는 SCC11 `lateral_position`에 해당하는 DBC 신호가 없다고 했으나, 실제 `hyundai_kia_generic.dbc`에는 `ACC_ObjLatPos : 24|9@1+ (0.1,-20)`가 있다. 기존 수동 디코더의 횡방향 비트 정의와 일치하므로 필드를 유지했다. DBC에는 `ObjValid : 16|1@1+`도 있으며 기존 분석기의 usable lead 조건에서 빠져 있었다.

`analyze_scc11.py`는 이제 ObjValid가 1이고 status가 양수이며 거리 범위가 적합한 프레임만 usable lead로 집계한다. ObjValid 프레임 개수를 별도 출력한다. 같은 수신 버스의 SCC12 `aReqRaw`, `aReqValue`를 DBC 정의대로 읽어 범위를 보고한다. 이 값은 순정 SCC 요청의 관측치이며 openpilot의 가속 제어 값으로 주입하지 않는다. 기존 SCC11 파서는 이미 openpilot `radar_interface.py`에 연결돼 있고 선택 리드 1개로 처리된다. 그 제어 경로의 유효성 게이트 변경에는 실제 차량 로그 재생이 필요하다.

합성 SCC11 유효·무효 및 SCC12 프레임을 포함한 분석기 테스트 5개가 통과했다. 기존 `.c4radar` 캡처 분석과 실차 주행 검증은 NAS 연결 후 진행할 수 있다.
