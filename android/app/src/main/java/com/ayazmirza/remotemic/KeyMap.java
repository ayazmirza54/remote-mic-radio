package com.ayazmirza.remotemic;

import java.util.Locale;

/** Turns "ctrl+shift+m" into a USB HID keyboard report (modifier bits + one key code). */
public final class KeyMap {
    public static final int CTRL = 0x01, SHIFT = 0x02, ALT = 0x04, WIN = 0x08;

    /** {modifiers, keycode} */
    public static int[] parse(String combo) {
        int mods = 0, key = 0;
        for (String raw : combo.toLowerCase(Locale.ROOT).split("\\+")) {
            String p = raw.trim();
            switch (p) {
                case "ctrl": case "control": mods |= CTRL; break;
                case "shift": mods |= SHIFT; break;
                case "alt": mods |= ALT; break;
                case "win": case "gui": case "meta": case "cmd": mods |= WIN; break;
                default:
                    if (key != 0) throw new IllegalArgumentException("only one key allowed: " + combo);
                    key = code(p);
            }
        }
        return new int[]{mods, key};
    }

    static int code(String k) {
        if (k.length() == 1) {
            char c = k.charAt(0);
            if (c >= 'a' && c <= 'z') return 0x04 + (c - 'a');
            if (c >= '1' && c <= '9') return 0x1E + (c - '1');
            if (c == '0') return 0x27;
        }
        switch (k) {
            case "space": return 0x2C;
            case "enter": return 0x28;
            case "esc": case "escape": return 0x29;
            case "tab": return 0x2B;
            case "backspace": return 0x2A;
            case "up": return 0x52;
            case "down": return 0x51;
            case "left": return 0x50;
            case "right": return 0x4F;
            default:
                if (k.matches("f([1-9]|1[0-2])")) return 0x3A + Integer.parseInt(k.substring(1)) - 1;
                throw new IllegalArgumentException("unknown key: " + k);
        }
    }

    /** 8-byte boot-keyboard style report body (report id is sent separately). */
    public static byte[] report(int mods, int key) {
        byte[] r = new byte[8];
        r[0] = (byte) mods;
        r[2] = (byte) key;
        return r;
    }

    private KeyMap() {}
}
