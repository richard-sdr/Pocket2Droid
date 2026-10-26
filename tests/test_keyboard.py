"""Device Emulator navigation-key SPI protocol through real QMP/QTest."""
import unittest, json
from pathlib import Path
from test_devices import DeviceTests

class KeyboardTests(DeviceTests):
    def keypad_sequences(self):
        return json.loads((Path(__file__).resolve().parents[1]/'android/tests/fixtures/sdk-keypad.json').read_text())['sequences']

    def test_keyboard_digits_make_break(self):
        self.write(0x59000020, 1 << 5)
        for name,seq in self.keypad_sequences().items():
            if name.startswith('f'): continue
            for phase in ('make','break'):
                self.key(name,phase=='make')
                self.assertEqual([self.pop() for _ in seq[phase]],seq[phase])
                self.assertEqual(self.pop(),0)

    def test_keyboard_fn_keys_make_break(self):
        """F1-F4 (soft keys, call/end) and F8-F9 (*/#) use AT scancodes directly."""
        self.write(0x59000020, 1 << 5)
        for name in ('f1','f2','f3','f4','f8','f9'):
            seq = self.keypad_sequences()[name]
            for phase in ('make','break'):
                self.key(name,phase=='make')
                self.assertEqual([self.pop() for _ in seq[phase]],seq[phase])
                self.assertEqual(self.pop(),0)

    def test_keyboard_keypad_with_other_keys(self):
        self.write(0x59000020, 1 << 5)
        expected=[]
        for name,down in [('up',True),('8',True),('f8',True),('8',False),('f8',False),('up',False)]:
            self.key(name,down)
            expected.extend(([0x6c if down else 0xec] if name=='up' else self.keypad_sequences()[name]['make' if down else 'break']))
        self.assertEqual([self.pop() for _ in range(len(expected)+1)],expected+[0])

    def key(self, name, down=True):
        self.qmp.request('input-send-event', {'events': [
            {'type': 'key', 'data': {'key': {'type': 'qcode', 'data': name}, 'down': down}}]})

    def pop(self):
        self.write(0x59000030, 0xff)
        return self.read(0x59000034)

    def test_keyboard_disabled_and_navigation_make_break(self):
        self.key('down')
        self.assertEqual(self.pop(), 0)
        self.write(0x59000020, 1 << 5)  # SPI1 interrupt mode.
        for name, code in [('up', 0x6c), ('down', 0x6a), ('left', 0x6d),
                           ('right', 0x6f), ('ret', 0x5a), ('esc', 0x29), ('spc', 0x6e),
                           ('tab', 0x0b), ('backspace', 0x69)]:
            self.key(name)
            self.key(name, False)
            self.assertTrue(self.read(0x4a000000) & 2)
            self.assertEqual(self.pop(), code)
            self.write(0x4a000000, 2)
            self.assertTrue(self.read(0x4a000000) & 2)
            self.assertEqual(self.pop(), code | 0x80)
            self.assertEqual(self.pop(), 0)

    def test_keyboard_spi_poll_does_not_consume_and_reset_drops_queue(self):
        self.write(0x59000020, 1 << 5)
        self.key('ret')
        self.write(0x59000030, 0)
        self.assertEqual(self.read(0x59000034), 0)
        self.assertEqual(self.pop(), 0x5a)
        self.key('down')
        self.qmp.request('system_reset')
        self.assertEqual(self.pop(), 0)

    def test_keyboard_full_queue_never_overwrites_unread_events(self):
        self.write(0x59000020, 1 << 5)
        for _ in range(32):
            self.key('tab')
        self.key('ret')  # Overflow must preserve the already queued sequence.
        for _ in range(32):
            self.assertEqual(self.pop(), 0x0b)
        self.assertEqual(self.pop(), 0)

    def test_keyboard_fn_keys_match_at_scancodes(self):
        """F1-F4/F8/F9 produce AT keyboard scancodes as single bytes.

        The original Device Emulator sent F1-F4 as Fn+digit three-byte sequences
        (0x63 + code + 0xe3), but Pocket2Droid sends raw AT scancodes because
        the WM5 guest ScanVK table recognises them directly, whereas the Fn
        scancode 0x63 is not universally handled by WM5 keyboard PDDs.
        """
        self.write(0x59000020, 1 << 5)
        # AT scancodes for F1-F4, F8-F9
        at_scancodes = [
            ('f1', 0x3b), ('f2', 0x3c), ('f3', 0x3d), ('f4', 0x3e),
            ('f8', 0x42), ('f9', 0x43),
        ]
        for name, fcode in at_scancodes:
            self.key(name)
            self.key(name, False)
            self.assertTrue(self.read(0x4a000000) & 2)
            # Single-byte make + single-byte break (no Fn prefix/suffix)
            self.assertEqual([self.pop(), self.pop()], [fcode, fcode | 0x80])
            self.assertEqual(self.pop(), 0)
        self.key('meta_l')
        self.key('meta_l', False)
        self.assertEqual([self.pop(), self.pop(), self.pop()], [0x60, 0xe0, 0])

    def test_keyboard_fn_key_preserves_queue_at_capacity(self):
        """F-key events are single-byte and queue like any other key."""
        self.write(0x59000020, 1 << 5)
        # Fill to 31, then add f1 → should queue fine
        for _ in range(31):
            self.key('tab')
        self.key('f1')
        # 31 tabs + 1 f1 make = 32 total, no overflow
        for _ in range(31):
            self.assertEqual(self.pop(), 0x0b)
        self.assertEqual(self.pop(), 0x3b)
        self.assertEqual(self.pop(), 0)

    def test_keyboard_fn_keys_preserve_other_held_keys(self):
        """Mixing navigation and F-keys keeps scancodes independent."""
        self.write(0x59000020, 1 << 5)
        for name, down in [('up', True), ('f3', True), ('f4', True),
                           ('f3', False), ('up', False), ('f4', False)]:
            self.key(name, down)
        # Each event is a single scancode (nav matrix or AT F-key)
        self.assertEqual([self.pop() for _ in range(7)],
                         [0x6c, 0x3d, 0x3e, 0xbd, 0xec, 0xbe, 0])

    def test_keyboard_direction_select_and_soft_key_together(self):
        """Simultaneous direction + select + soft key emits independent scancodes."""
        self.write(0x59000020, 1 << 5)
        self.qmp.request('input-send-event', {'events': [
            {'type': 'key', 'data': {'key': {'type': 'qcode', 'data': name}, 'down': True}}
            for name in ['left', 'ret', 'f1']]})
        self.key('ret', False)
        self.key('f1', False)
        self.key('left', False)
        # left(0x6d), ret(0x5a), f1(0x3b), ret_break(0xda), f1_break(0xbb), left_break(0xed)
        self.assertEqual([self.pop() for _ in range(7)],
                         [0x6d, 0x5a, 0x3b, 0xda, 0xbb, 0xed, 0])

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(KeyboardTests(name) for name in KeyboardTests.__dict__
                              if name.startswith('test_keyboard_'))

if __name__ == '__main__':
    unittest.main()
