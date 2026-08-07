import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

const String xfyunHost = 'ws-api.xf-yun.com';
const String xfyunPath = '/v1/private/simult_interpretation';

/// 生成讯飞同声传译 WebSocket 鉴权地址（与桌面版 HMAC-SHA256 算法一致）。
String buildXfyunAuthUrl({
  required String appId,
  required String apiKey,
  required String apiSecret,
  DateTime? now,
}) {
  final DateTime date = (now ?? DateTime.now()).toUtc();
  final String dateHeader = HttpDate.format(date);
  final String signatureOrigin =
      'host: $xfyunHost\ndate: $dateHeader\nGET $xfyunPath HTTP/1.1';
  final Hmac hmac = Hmac(sha256, utf8.encode(apiSecret));
  final String signature = base64
      .encode(hmac.convert(utf8.encode(signatureOrigin)).bytes)
      .trim();
  final String authorizationOrigin =
      'api_key="$apiKey", algorithm="hmac-sha256", '
      'headers="host date request-line", signature="$signature"';
  final String authorization = base64.encode(utf8.encode(authorizationOrigin));

  final Uri uri = Uri(
    scheme: 'wss',
    host: xfyunHost,
    path: xfyunPath,
    queryParameters: {
      'authorization': authorization,
      'date': dateHeader,
      'host': xfyunHost,
    },
  );
  return uri.toString();
}
