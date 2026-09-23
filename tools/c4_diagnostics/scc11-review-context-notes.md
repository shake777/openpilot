# K7 SCC11 판단 기록

2026-09-23. `hyundai_kia_generic.dbc`의 SCC11에는 `ACC_ObjLatPos 24|9@1+ (0.1,-20)`가 존재한다. 첨부 문서의 횡방향 신호 부재 주장은 사실이 아니므로 필드를 유지한다. `ObjValid 16|1@1+`는 분석기에서 누락됐다. CAN FD `hyundaicanfd.py`의 가속 밴드 주석은 K7 non-CANFD 원인의 증거가 아니다. NAS 경로 조회는 네트워크 경로 오류 53으로 실패했다.
