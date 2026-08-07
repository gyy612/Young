import 'dart:async';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/xfyun_client.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const IsmolarApp());
}

class IsmolarApp extends StatelessWidget {
  const IsmolarApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ísmolar 同声传译',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF2E6BE6),
          brightness: Brightness.light,
        ),
        fontFamily: 'PingFang SC',
      ),
      home: const CredentialsScreen(),
    );
  }
}

class AppSettings {
  AppSettings({
    this.appId = '',
    this.apiKey = '',
    this.apiSecret = '',
    this.deepseekKey = '',
    this.direction = 'zh_en',
  });

  final String appId;
  final String apiKey;
  final String apiSecret;
  final String deepseekKey;
  final String direction;

  bool get ready =>
      appId.trim().isNotEmpty &&
      apiKey.trim().isNotEmpty &&
      apiSecret.trim().isNotEmpty &&
      (direction != 'zh_en' || deepseekKey.trim().isNotEmpty);

  static Future<AppSettings> load() async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    return AppSettings(
      appId: prefs.getString('app_id') ?? '',
      apiKey: prefs.getString('api_key') ?? '',
      apiSecret: prefs.getString('api_secret') ?? '',
      deepseekKey: prefs.getString('deepseek_api_key') ?? '',
      direction: prefs.getString('direction') ?? 'zh_en',
    );
  }

  Future<void> save() async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    await prefs.setString('app_id', appId.trim());
    await prefs.setString('api_key', apiKey.trim());
    await prefs.setString('api_secret', apiSecret.trim());
    await prefs.setString('deepseek_api_key', deepseekKey.trim());
    await prefs.setString('direction', direction);
  }
}

class CredentialsScreen extends StatefulWidget {
  const CredentialsScreen({super.key});

  @override
  State<CredentialsScreen> createState() => _CredentialsScreenState();
}

class _CredentialsScreenState extends State<CredentialsScreen> {
  final TextEditingController _appId = TextEditingController();
  final TextEditingController _apiKey = TextEditingController();
  final TextEditingController _apiSecret = TextEditingController();
  final TextEditingController _deepseekKey = TextEditingController();
  String _direction = 'zh_en';
  bool _loaded = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final AppSettings settings = await AppSettings.load();
    if (!mounted) return;
    setState(() {
      _appId.text = settings.appId;
      _apiKey.text = settings.apiKey;
      _apiSecret.text = settings.apiSecret;
      _deepseekKey.text = settings.deepseekKey;
      _direction = settings.direction;
      _loaded = true;
    });
  }

  Future<void> _saveAndContinue() async {
    final AppSettings settings = AppSettings(
      appId: _appId.text,
      apiKey: _apiKey.text,
      apiSecret: _apiSecret.text,
      deepseekKey: _deepseekKey.text,
      direction: _direction,
    );
    if (!settings.ready) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('请填写讯飞三件套；自动/英译中还需 DeepSeek Key')),
      );
      return;
    }
    await settings.save();
    if (!mounted) return;
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => SessionScreen(settings: settings),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
          children: [
            const Text(
              'ísmolar 同声传译',
              style: TextStyle(fontSize: 28, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            Text(
              '密钥明文显示，仅保存在本机',
              style: TextStyle(color: Colors.grey.shade600),
            ),
            const SizedBox(height: 24),
            _field(_appId, '讯飞 APPID'),
            const SizedBox(height: 12),
            _field(_apiKey, '讯飞 APIKey'),
            const SizedBox(height: 12),
            _field(_apiSecret, '讯飞 APISecret'),
            const SizedBox(height: 12),
            _field(_deepseekKey, 'DeepSeek API Key'),
            const SizedBox(height: 20),
            const Text('翻译方向', style: TextStyle(fontWeight: FontWeight.w600)),
            const SizedBox(height: 8),
            SegmentedButton<String>(
              segments: const [
                ButtonSegment(value: 'auto', label: Text('自动')),
                ButtonSegment(value: 'zh_en', label: Text('中译英')),
                ButtonSegment(value: 'en_zh', label: Text('英译中')),
              ],
              selected: {_direction},
              onSelectionChanged: (selection) {
                setState(() => _direction = selection.first);
              },
            ),
            const SizedBox(height: 32),
            FilledButton(
              onPressed: _loaded ? _saveAndContinue : null,
              style: FilledButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 16),
                textStyle: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
              ),
              child: const Text('进入传译'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(TextEditingController controller, String label) {
    return TextField(
      controller: controller,
      decoration: InputDecoration(
        labelText: label,
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
      ),
    );
  }
}

class SessionScreen extends StatefulWidget {
  const SessionScreen({super.key, required this.settings});

  final AppSettings settings;

  @override
  State<SessionScreen> createState() => _SessionScreenState();
}

class _SessionScreenState extends State<SessionScreen> {
  XfyunInterpreter? _client;
  String _status = '准备就绪';
  String _chinese = '';
  String _english = '';
  bool _running = false;

  void _onEvent(XfyunEvent event) {
    if (!mounted) return;
    switch (event.type) {
      case 'status':
        setState(() => _status = '${event.data['text']}');
        break;
      case 'subtitles':
        setState(() {
          _chinese = '${event.data['chinese'] ?? ''}';
          _english = '${event.data['english'] ?? ''}';
        });
        break;
      case 'error':
        setState(() => _status = '${event.data['text']}');
        break;
    }
  }

  Future<void> _toggle() async {
    if (_running) {
      await _client?.stop();
      setState(() {
        _running = false;
        _status = '已停止';
      });
      return;
    }
    final XfyunInterpreter client = XfyunInterpreter(
      appId: widget.settings.appId,
      apiKey: widget.settings.apiKey,
      apiSecret: widget.settings.apiSecret,
      direction: widget.settings.direction,
      deepseekApiKey: widget.settings.deepseekKey,
      onEvent: _onEvent,
    );
    setState(() {
      _client = client;
      _running = true;
      _chinese = '';
      _english = '';
    });
    try {
      await client.start();
    } catch (error) {
      setState(() {
        _running = false;
        _status = '$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('同声传译')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                _status,
                style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
              ),
              const SizedBox(height: 16),
              Expanded(
                child: Container(
                  decoration: BoxDecoration(
                    color: Colors.grey.shade100,
                    borderRadius: BorderRadius.circular(16),
                  ),
                  padding: const EdgeInsets.all(16),
                  child: SingleChildScrollView(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          _chinese.isEmpty ? '（等待识别）' : _chinese,
                          style: const TextStyle(fontSize: 22, height: 1.4),
                        ),
                        const Divider(height: 28),
                        Text(
                          _english.isEmpty ? '（等待翻译）' : _english,
                          style: TextStyle(
                            fontSize: 18,
                            height: 1.4,
                            color: Colors.grey.shade800,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: _toggle,
                style: FilledButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 18),
                  backgroundColor:
                      _running ? Colors.red.shade400 : null,
                  textStyle: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                child: Text(_running ? '停止' : '开始'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
