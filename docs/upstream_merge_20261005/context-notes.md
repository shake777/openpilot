# 결정과 근거.

- 사용자가 원 제작자 변경을 현재 브랜치에 머지하라고 명시했다.
- 현재 carrot-wip-custom의 기준 HEAD는 b5e58795이며 upstream은 https://github.com/ajouatom/openpilot.git이다.
- 기존 추적 파일 변경은 없지만 미추적 체크리스트·조사 자료가 있으므로 이를 수정하거나 삭제하지 않는다.
- 원격 푸시와 C4 기기 업데이트는 아직 실행하지 않는다. 코드 머지·로컬 검증과 실제 차량 검증은 구분한다.
- 원 제작자의 56개 추가 커밋을 포함해 211개 파일이 변경되었다. 7개 충돌은 수동 병합했다.
- 레이더 안내는 새 SCC 전용 모드 0 정책을 반영하면서 K7 전용 모드 4·5와 제어 제한 설명을 보존했다. 한국어 직접 계기판 송신 안내도 포함했다.
- 레이더 웹 출력은 기록된 모드를 사용하도록 바꾸면서 classic238 원시 객체 표시 메타데이터를 보존했다. 화면 렌더러에는 K7 전용 표시와 새 native 통계 표시를 모두 유지했다.
- git diff --cached --check, 변경 Python 119개 AST 문법 검증과 설정 JSON 파싱이 통과했다.
- Windows 문서·화면·조향 관련 테스트 71개가 통과했다. Linux SCC·레이더 예측·radard·조향·route-vault 테스트 689개도 통과했다. 두 실행의 일부 조향 테스트는 중복되므로 합계를 고유 테스트 수로 해석하지 않는다.
- 이 PC의 원래 한글 경로에서는 Capnp 로더가 /include/c++.capnp를 읽지 못했다. ASCII 디렉터리에 복사한 동일 바이트 스키마는 정상 파싱됐다. 로컬 시험 로더는 각 스키마 SHA-256 일치를 확인하고 실제 Capnp 파서에 그 복사본을 제공했다. 생산 코드나 메시지 구조는 바꾸지 않았다.
- Linux 시험 호스트는 Python 3.14.4이며 해당 호스트용 wheel이 없어 시험 의존성만 pycapnp 2.2.4를 사용했다. 생산 requirements의 2.2.2는 유지했다. native Cython/Panda 전체 빌드·C4 설치·차량 주행 검증을 대신하지 않는다.
- 재현 도구와 의존성은 .analysis/scratch/20261005-upstream-merge/verify_linux.py 및 .analysis/scratch/20261005-upstream-merge-linux-deps에 보관한다. 기존 조사 자료를 삭제하거나 변경하지 않았다.
- 레이더 코드가 포함되어 NAS replay 동기화도 필요하다. 원격 공개 후 기존 Carrot Routes image 자동 경로의 커밋·배포·실제 결과 재계산을 확인해야 하며 아직 실행하지 않았다.
