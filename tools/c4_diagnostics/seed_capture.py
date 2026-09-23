# 기존 단일 보안 seed 요청의 반환 데이터와 순서를 기록한다.
import time


def capture_seed(uds_client, report):
  exchange = {'request_hex': '2701', 'status': 'pending',
              'started_monotonic': time.monotonic(), 'response_source': 'uds_client_payload'}
  report.setdefault('security_exchanges', []).append(exchange)
  try:
    seed = uds_client.security_access(0x01)
  except Exception as error:
    exchange.update(status='error', error_type=type(error).__name__)
    if hasattr(error, 'error_code'):
      exchange['nrc'] = f'0x{error.error_code:02x}'
    raise
  else:
    exchange.update(status='accepted', seed_hex=seed.hex())
    report['seed_hex'] = seed.hex()
    return seed
  finally:
    exchange['elapsed_s'] = time.monotonic() - exchange['started_monotonic']
