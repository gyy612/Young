import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'audio_capture.dart';
import 'deepseek.dart';
import 'xfyun_auth.dart';

/// 事件类型：status / subtitles / error / closed
class XfyunEvent {
  const XfyunEvent(this.type, [this.data = const {}]);

  final String type;
  final Map<String, dynamic> data;
}

class XfyunInterpreter {
  XfyunInterpreter({
    required this.appId,
    required this.apiKey,
    required this.apiSecret,
    required this.onEvent,
    this.direction = 'zh_en',
    String deepseekApiKey = '',
    String deepseekModel = 'deepseek-v4-flash',
    List<List<String>> glossary = const [],
    String referenceText = '',
    List<Map<String, dynamic>> initialSegments = const [],
  })  : deepseek = DeepSeekClient(
          apiKey: deepseekApiKey,
          model: deepseekModel,
          glossary: glossary,
          referenceText: referenceText,
        ),
        _initialSegments = [for (final s in initialSegments) Map.of(s)];

  final String appId;
  final String apiKey;
  final String apiSecret;
  final String direction;
  final DeepSeekClient deepseek;
  final void Function(XfyunEvent) onEvent;
  final List<Map<String, dynamic>> _initialSegments;

  final AudioCapture _audio = AudioCapture();
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _messageSub;
  StreamSubscription<Uint8List>? _audioSub;
  bool _running = false;
  bool _stopping = false;
  int _seq = 0;
  int _retryCount = 0;
  Timer? _reconnectTimer;

  // 有序段落（与桌面版一致，用于浮窗显示和最终文档）。
  final List<Map<String, dynamic>> _orderedSegments = [];
  int _segmentSeq = 0;
  String _interimCn = '';
  String _interimEn = '';
  final Map<int, String> _asrSegments = {};
  final List<String> _finalCn = [];
  final List<String> _finalEn = [];

  bool get isRunning => _running;

  void _emit(String type, [Map<String, dynamic> data = const {}]) {
    onEvent(XfyunEvent(type, data));
  }

  String get _streamFrom => direction == 'en_zh' ? 'en' : 'cn';
  String get _streamTo => direction == 'en_zh' ? 'cn' : 'en';
  int get _languageType => direction == 'en_zh' ? 3 : 1;

  Map<String, dynamic> _makePayload(int status, Uint8List audio) {
    return {
      'header': {'app_id': appId, 'status': status},
      'parameter': {
        'ist': {
          'accent': 'mandarin',
          'domain': 'ist_ed_open',
          'language': 'zh_cn',
          'language_type': _languageType,
          'vto': 15000,
          'eos': 150000,
        },
        'streamtrans': {'from': _streamFrom, 'to': _streamTo},
        'tts': {
          'vcn': 'x2_catherine',
          'tts_results': {
            'encoding': 'raw',
            'sample_rate': 16000,
            'channels': 1,
            'bit_depth': 16,
            'frame_size': 0,
          },
        },
      },
      'payload': {
        'data': {
          'audio': base64.encode(audio),
          'encoding': 'raw',
          'sample_rate': 16000,
          'seq': _seq,
          'status': status,
        },
      },
    };
  }

  Future<void> start() async {
    if (_running) return;
    if (appId.trim().isEmpty || apiKey.trim().isEmpty || apiSecret.trim().isEmpty) {
      throw StateError('讯飞 APPID、APIKey 和 APISecret 不能为空');
    }
    if ((direction == 'auto' || direction == 'en_zh') &&
        deepseek.apiKey.trim().isEmpty) {
      throw StateError('自动识别和英译中模式需要 DeepSeek API Key');
    }

    _running = true;
    _stopping = false;
    _seq = 0;
    _asrSegments.clear();
    _finalCn.clear();
    _finalEn.clear();
    _orderedSegments.clear();
    _segmentSeq = 0;
    _orderedSegments.addAll(
      [for (final s in _initialSegments) Map.of(s)],
    );
    _segmentSeq = _orderedSegments.length;
    _interimCn = '';
    _interimEn = '';

    _emit('status', {'text': '正在连接讯飞服务…', 'state': 'connecting'});
    await _connect();
  }

  Future<void> _connect() async {
    try {
      final String url = buildXfyunAuthUrl(
        appId: appId,
        apiKey: apiKey,
        apiSecret: apiSecret,
      );
      final WebSocketChannel channel = WebSocketChannel.connect(Uri.parse(url));
      _channel = channel;
      _messageSub = channel.stream.listen(
        _onMessage,
        onDone: _onDisconnected,
        onError: (Object error) => _onDisconnected(),
        cancelOnError: true,
      );
      await _startAudioLoop(channel);
      _retryCount = 0;
      _emit('status', {'text': '已连接，正在监听麦克风', 'state': 'connected'});
    } catch (error) {
      _onDisconnected();
    }
  }

  Future<void> _startAudioLoop(WebSocketChannel channel) async {
    final Stream<Uint8List> stream = await _audio.start();
    bool first = true;
    _audioSub = stream.listen((Uint8List chunk) {
      if (!_running) return;
      final int status = first ? 0 : 1;
      first = false;
      channel.sink.add(
        jsonEncode(_makePayload(status, chunk)),
      );
    }, onError: (Object error) {
      if (_running) {
        _emit('error', {'text': '麦克风采集失败：$error'});
      }
    });
  }

  void _onMessage(dynamic rawMessage) {
    if (rawMessage is! String) return;
    final Map<String, dynamic> packet;
    try {
      packet = jsonDecode(rawMessage) as Map<String, dynamic>;
    } catch (_) {
      return;
    }
    final Map<String, dynamic> header =
        (packet['header'] as Map?)?.cast<String, dynamic>() ?? const {};
    final int code = int.tryParse('${header['code'] ?? 0}') ?? 0;
    if (code != 0) {
      _emit('error', {'code': code, 'text': '讯飞返回错误 $code：${header['message']}'});
      stop();
      return;
    }
    final Map<String, dynamic> payload =
        (packet['payload'] as Map?)?.cast<String, dynamic>() ?? const {};
    final dynamic recognition = payload['recognition_results'];
    if (recognition is Map) {
      _handleAsr(recognition.cast<String, dynamic>());
    }
    final dynamic translation = payload['streamtrans_results'];
    if (translation is Map) {
      _handleTranslation(translation.cast<String, dynamic>());
    }
  }

  Map<String, dynamic> _decodeJsonText(String text) {
    final dynamic decoded = jsonDecode(text);
    return decoded as Map<String, dynamic>;
  }

  void _handleAsr(Map<String, dynamic> result) {
    final Map<String, dynamic> decoded;
    try {
      decoded = _decodeJsonText('${result['text']}');
    } catch (_) {
      return;
    }
    final List<String> words = [];
    for (final dynamic item in (decoded['ws'] as List? ?? const [])) {
      final List<dynamic> candidates = (item as Map)['cw'] as List? ?? const [];
      if (candidates.isNotEmpty) {
        words.add('${(candidates.first as Map)['w']}');
      }
    }
    final String text = words.join();
    final int sn = int.tryParse('${decoded['sn'] ?? (_asrSegments.length + 1)}') ??
        (_asrSegments.length + 1);
    if (decoded['pgs'] == 'rpl') {
      final List<dynamic> rg = decoded['rg'] as List? ?? const [];
      if (rg.length == 2) {
        final int start = int.tryParse('${rg[0]}') ?? 0;
        final int end = int.tryParse('${rg[1]}') ?? 0;
        _asrSegments.removeWhere((key, _) => key >= start && key <= end);
      }
    }
    _asrSegments[sn] = text;
    final StringBuffer full = StringBuffer();
    final List<int> keys = _asrSegments.keys.toList()..sort();
    for (final int key in keys) {
      full.write(_asrSegments[key]);
    }
    final String sentence = full.toString().trim();
    final String sourceLanguage = direction == 'auto'
        ? detectSourceLanguage(sentence)
        : (direction == 'en_zh' ? 'en' : 'zh');
    final int recognitionStatus = int.tryParse('${result['status'] ?? 1}') ?? 1;
    final bool sentenceFinished =
        decoded['sub_end'] == true ||
        decoded['ls'] == true ||
        recognitionStatus == 2;

    if (sourceLanguage == 'zh') {
      _interimCn = full.toString();
      _emitSubtitles();
      if (sentenceFinished) _asrSegments.clear();
    } else {
      _interimEn = sentence;
      _emitSubtitles();
      if (sentenceFinished && sentence.isNotEmpty) {
        _asrSegments.clear();
        // 简化版：英文句子先上屏，精修由 DeepSeek 在提交时执行。
        _commitEnglish(sentence);
      }
    }
  }

  void _commitEnglish(String sentence) {
    final int index = _appendSegment(sentence, '', 'en');
    if (deepseek.apiKey.trim().isNotEmpty) {
      unawaited(_translateEnglish(sentence, index));
    }
  }

  Future<void> _translateEnglish(String sentence, int index) async {
    try {
      final String translation =
          await deepseek.translate(sentence, sourceLang: 'en', targetLang: 'zh');
      _updateSegment(index, translation);
      _emitSubtitles();
    } catch (_) {
      // DeepSeek 失败时保留讯飞快译结果或空段。
    }
  }

  void _handleTranslation(Map<String, dynamic> result) {
    final Map<String, dynamic> decoded;
    try {
      decoded = _decodeJsonText('${result['text']}');
    } catch (_) {
      return;
    }
    final String src = '${decoded['src'] ?? ''}'.trim();
    final String dst = '${decoded['dst'] ?? ''}'.trim();
    final bool isFinal = '${decoded['is_final'] ?? 0}' == '1';

    if (direction == 'en_zh') {
      // 英译中：讯飞快译草稿 + DeepSeek 精修（简化版直接上屏草稿）。
      if (isFinal) {
        if (src.isNotEmpty) {
          final int index = _appendSegment(src, dst, 'en');
          unawaited(_translateEnglish(src, index));
        }
      } else {
        _interimEn = src;
        _interimCn = dst;
      }
    } else {
      if (isFinal) {
        if (src.isNotEmpty) _finalCn.add(src);
        _interimCn = '';
        if (dst.isNotEmpty) _finalEn.add(dst);
        _interimEn = '';
        _appendSegment(src, dst, 'zh');
      } else {
        _interimCn = src;
        _interimEn = dst;
      }
    }
    _emitSubtitles();
  }

  int _appendSegment(String source, String translation, String language) {
    _segmentSeq += 1;
    _orderedSegments.add({
      'id': _segmentSeq.toString().padLeft(4, '0'),
      'source': source,
      'translation': translation,
      'source_language': language,
      'status': translation.isEmpty ? 'pending' : 'complete',
      'error': '',
    });
    return _orderedSegments.length - 1;
  }

  void _updateSegment(int index, String translation) {
    if (index >= 0 && index < _orderedSegments.length) {
      _orderedSegments[index]['translation'] = translation;
      _orderedSegments[index]['status'] = translation.isEmpty ? 'pending' : 'complete';
    }
  }

  List<Map<String, dynamic>> _segmentSnapshot() {
    return [for (final s in _orderedSegments) Map.of(s)];
  }

  void _emitSubtitles() {
    final String chinese = [
      ..._finalCn,
      if (_interimCn.isNotEmpty) _interimCn,
    ].join('\n');
    final String english = [
      ..._finalEn,
      if (_interimEn.isNotEmpty) _interimEn,
    ].join('\n');
    _emit('subtitles', {
      'chinese': chinese,
      'english': english,
      'segments': _segmentSnapshot(),
    });
  }

  Future<void> _onDisconnected() async {
    _messageSub?.cancel();
    _messageSub = null;
    await _audioSub?.cancel();
    _audioSub = null;
    unawaited(_audio.stop());
    if (!_running || _stopping) return;
    if (_retryCount < 3) {
      _retryCount += 1;
      final int waitMs = 1500 * _retryCount;
      _emit('status', {'text': '网络连接中断，正在自动重连（$_retryCount/3）…', 'state': 'reconnecting'});
      _reconnectTimer = Timer(Duration(milliseconds: waitMs), () {
        if (_running && !_stopping) unawaited(_connect());
      });
    } else {
      _emit('error', {
        'text': '网络连接中断，已重试 3 次仍未恢复，已识别内容不会丢失。',
      });
      stop();
    }
  }

  Future<void> stop() async {
    if (!_running) return;
    _running = false;
    _stopping = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _messageSub?.cancel();
    _messageSub = null;
    await _audioSub?.cancel();
    _audioSub = null;
    await _audio.stop();
    _channel?.sink.close();
    _channel = null;
    _emit('status', {'text': '已停止', 'state': 'stopped'});
  }
}
