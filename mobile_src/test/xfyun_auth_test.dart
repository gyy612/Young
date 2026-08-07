import 'package:flutter_test/flutter_test.dart';
import 'package:ismolar_mobile/core/xfyun_auth.dart';

void main() {
  test('讯飞鉴权签名与桌面版 Python 实现一致', () {
    final String url = buildXfyunAuthUrl(
      appId: 'test_app_id',
      apiKey: 'test_api_key',
      apiSecret: 'test_api_secret',
      now: DateTime.utc(2026, 8, 8),
    );
    final Uri uri = Uri.parse(url);
    expect(uri.scheme, 'wss');
    expect(uri.host, 'ws-api.xf-yun.com');
    expect(uri.path, '/v1/private/simult_interpretation');
    expect(uri.queryParameters['date'], 'Sat, 08 Aug 2026 00:00:00 GMT');
    expect(uri.queryParameters['host'], 'ws-api.xf-yun.com');
    // 期望授权串由桌面版同一算法（Python）生成。
    expect(
      uri.queryParameters['authorization'],
      'YXBpX2tleT0idGVzdF9hcGlfa2V5IiwgYWxnb3JpdGhtPSJobWFjLXNoYTI1NiIsIGhlYWRlcnM9Imhvc3QgZGF0ZSByZXF1ZXN0LWxpbmUiLCBzaWduYXR1cmU9InVSM09KQmtBMHlwclNNL3ZOR1RoblhUMXRMMzZLVXYvV3NHWGlKOVMrb009Ig==',
    );
  });
}
