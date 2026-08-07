import 'dart:convert';

import 'package:http/http.dart' as http;

/// 参考桌面版的中英文判断：按 CJK 与拉丁字符比例推断。
String detectSourceLanguage(String text) {
  final int cjk = RegExp(r'[\u3400-\u9fff]').allMatches(text).length;
  final int latin = RegExp(r'[A-Za-z]').allMatches(text).length;
  if (cjk == 0 && latin > 0) return 'en';
  if (latin == 0 && cjk > 0) return 'zh';
  return cjk >= (latin * 0.28).round().clamp(1, 1 << 31) ? 'zh' : 'en';
}

/// 去掉空白与标点，保留中英文和数字，便于 ASR 与稿件句子对碰。
String normalizeMatch(String text) {
  return text.replaceAll(RegExp(r'[\W_]+', unicode: true), '').toLowerCase();
}

/// 简单 Levenshtein 相似度，等价于桌面版 difflib 的用途。
double similarity(String a, String b) {
  if (a == b) return 1.0;
  if (a.isEmpty || b.isEmpty) return 0.0;
  final List<int> prev = List<int>.generate(b.length + 1, (i) => i);
  for (int i = 1; i <= a.length; i++) {
    final List<int> cur = List<int>.filled(b.length + 1, 0);
    cur[0] = i;
    for (int j = 1; j <= b.length; j++) {
      final int cost = a.codeUnitAt(i - 1) == b.codeUnitAt(j - 1) ? 0 : 1;
      cur[j] = [cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost].reduce(
        (x, y) => x < y ? x : y,
      );
    }
    prev
      ..clear()
      ..addAll(cur);
  }
  final int dist = prev[b.length];
  return 1.0 - dist / (a.length > b.length ? a.length : b.length);
}

/// 翻译记忆：内存热缓存 + 精确/模糊匹配（磁盘持久化后续接入 sqflite）。
class TranslationMemory {
  static const int maxEntries = 800;
  final Map<String, String> _cache = {};

  String? lookup(String text, String targetLang) {
    final String key = normalizeMatch(text);
    if (key.length < 2) return null;
    final String? exact = _cache['$key\x00$targetLang'];
    if (exact != null) return exact;
    if (key.length < 4) return null;

    String? best;
    double bestRatio = 0.0;
    _cache.forEach((cachedKey, value) {
      final int sep = cachedKey.indexOf('\x00');
      if (sep < 0) return;
      final String cached = cachedKey.substring(0, sep);
      final String lang = cachedKey.substring(sep + 1);
      if (lang != targetLang || cached.length < 4) return;
      if (cached.length / key.length > 2.2 ||
          key.length / cached.length > 2.2) {
        return;
      }
      final double ratio = similarity(key, cached);
      if (ratio > bestRatio) {
        bestRatio = ratio;
        best = value;
      }
    });
    return bestRatio >= 0.86 ? best : null;
  }

  void store(String text, String targetLang, String translation) {
    final String key = normalizeMatch(text);
    if (key.length < 2 || translation.trim().isEmpty) return;
    if (_cache.length >= maxEntries) {
      final String first = _cache.keys.first;
      _cache.remove(first);
    }
    _cache['$key\x00$targetLang'] = translation.trim();
  }
}

class DeepSeekClient {
  DeepSeekClient({
    required this.apiKey,
    this.model = 'deepseek-v4-flash',
    List<List<String>> glossary = const [],
    String referenceText = '',
  })  : _glossary = [for (final e in glossary) [e[0], e[1]]],
        // ignore: prefer_initializing_formals
        _referenceText = referenceText;

  final String apiKey;
  final String model;
  final List<List<String>> _glossary;
  final String _referenceText;
  final TranslationMemory memory = TranslationMemory();

  bool get hasContext => _glossary.isNotEmpty || _referenceText.trim().isNotEmpty;

  String _systemPrompt(String text, String sourceLang, String targetLang) {
    final StringBuffer sections = StringBuffer('你是专业同声传译。');
    if (_glossary.isNotEmpty) {
      sections.write(' 固定译法（必须优先使用）：');
      for (final entry in _glossary) {
        sections.write('${entry[0]}=${entry[1]}；');
      }
    }
    if (_referenceText.trim().isNotEmpty) {
      final int maxChars = 6000;
      final String excerpt = _referenceText.trim().length > maxChars
          ? _referenceText.trim().substring(0, maxChars)
          : _referenceText.trim();
      sections
        ..write(' 以下是参考稿件相关内容，仅用于理解上下文、人名、职位、'
            '专业表达和预期措辞，不要照抄与当前原文无关的句子：')
        ..write(excerpt);
    }
    sections.write(' 只输出译文本身，不要解释。');
    return sections.toString();
  }

  String _applyGlossary(String text, String translated) {
    String result = translated;
    for (final entry in _glossary) {
      if (text.toLowerCase().contains(entry[0].toLowerCase())) {
        result = result.replaceAll(
          RegExp(RegExp.escape(entry[0]), caseSensitive: false),
          entry[1],
        );
      }
    }
    return result;
  }

  Future<String> translate(
    String text, {
    required String sourceLang,
    required String targetLang,
  }) async {
    final String trimmed = text.trim();
    if (trimmed.isEmpty) return '';
    if (apiKey.trim().isEmpty) {
      throw StateError('未配置 DeepSeek API Key');
    }

    final String? cached = memory.lookup(trimmed, targetLang);
    if (cached != null) return _applyGlossary(trimmed, cached);

    final http.Response response = await http
        .post(
          Uri.parse('https://api.deepseek.com/chat/completions'),
          headers: {
            'Authorization': 'Bearer ${apiKey.trim()}',
            'Content-Type': 'application/json',
          },
          body: jsonEncode({
            'model': model,
            'thinking': {'type': 'disabled'},
            'temperature': 0.05,
            'max_tokens': 1000,
            'messages': [
              {
                'role': 'system',
                'content': _systemPrompt(trimmed, sourceLang, targetLang),
              },
              {'role': 'user', 'content': trimmed},
            ],
          }),
        )
        .timeout(const Duration(seconds: 45));

    if (response.statusCode != 200) {
      throw StateError('DeepSeek HTTP ${response.statusCode}: '
          '${response.body.substring(0, response.body.length.clamp(0, 300))}');
    }
    final Map<String, dynamic> payload =
        jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    final String translated = ((payload['choices'] as List).first
            as Map<String, dynamic>)['message']['content']
        .toString()
        .trim();
    final String result = _applyGlossary(trimmed, translated);
    memory.store(trimmed, targetLang, result);
    return result;
  }
}
