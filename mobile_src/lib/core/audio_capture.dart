import 'dart:async';
import 'dart:typed_data';

import 'package:record/record.dart';

/// 麦克风 PCM 流采集：16kHz、16 位、单声道（与讯飞协议一致）。
class AudioCapture {
  final AudioRecorder _recorder = AudioRecorder();
  StreamSubscription<Uint8List>? _subscription;

  Future<Stream<Uint8List>> start() async {
    if (!await _recorder.hasPermission()) {
      throw StateError('需要麦克风权限才能进行实时识别');
    }
    return _recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
      ),
    );
  }

  Future<void> stop() async {
    await _subscription?.cancel();
    _subscription = null;
    try {
      await _recorder.stop();
    } catch (_) {
      // 未在录制时忽略。
    }
  }

  void dispose() {
    _recorder.dispose();
  }
}
