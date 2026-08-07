import 'package:flutter_test/flutter_test.dart';
import 'package:ismolar_mobile/main.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('设置页正常渲染', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const IsmolarApp());
    expect(find.text('ísmolar 同声传译'), findsOneWidget);
    expect(find.text('进入传译'), findsOneWidget);
  });
}
