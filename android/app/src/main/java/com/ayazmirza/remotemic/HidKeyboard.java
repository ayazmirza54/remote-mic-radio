package com.ayazmirza.remotemic;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothHidDevice;
import android.bluetooth.BluetoothHidDeviceAppSdpSettings;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Makes the phone look like a Bluetooth keyboard to the laptop (Android HID Device profile).
 * Windows needs no software: pair once in Settings > Bluetooth & devices.
 */
@SuppressLint("MissingPermission") // permissions are requested in MainActivity before start()
public class HidKeyboard {

    public interface Listener { void onChanged(); }

    private static final int REPORT_ID = 1;

    // Standard keyboard: report id 1, modifier byte, reserved byte, 6 key slots.
    private static final byte[] DESCRIPTOR = {
            0x05, 0x01, 0x09, 0x06, (byte) 0xA1, 0x01,
            (byte) 0x85, REPORT_ID,
            0x05, 0x07, 0x19, (byte) 0xE0, 0x29, (byte) 0xE7, 0x15, 0x00, 0x25, 0x01,
            0x75, 0x01, (byte) 0x95, 0x08, (byte) 0x81, 0x02,
            (byte) 0x95, 0x01, 0x75, 0x08, (byte) 0x81, 0x01,
            (byte) 0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
            0x05, 0x07, 0x19, 0x00, 0x29, 0x65, (byte) 0x81, 0x00,
            (byte) 0xC0
    };

    private final Context ctx;
    private final Listener listener;
    private final BluetoothAdapter adapter;
    private final SharedPreferences prefs;
    // One thread for all HID traffic so key-down / key-up never reorder.
    private final ExecutorService io = Executors.newSingleThreadExecutor();

    private BluetoothHidDevice hid;
    private BluetoothDevice host;      // connected computer
    private boolean registered;
    private int connState = BluetoothProfile.STATE_DISCONNECTED;
    private boolean holding;
    private String error = "";
    private String lastSent = "";

    public HidKeyboard(Context ctx, Listener listener) {
        this.ctx = ctx.getApplicationContext();
        this.listener = listener;
        BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        this.adapter = bm == null ? null : bm.getAdapter();
        this.prefs = ctx.getSharedPreferences("rm", Context.MODE_PRIVATE);
    }

    // ------------------------------------------------------------------ lifecycle
    public void start() {
        if (adapter == null) { error = "This phone has no Bluetooth."; changed(); return; }
        if (!adapter.isEnabled()) { error = "Turn Bluetooth on."; changed(); return; }
        if (hid != null) return;
        boolean ok = adapter.getProfileProxy(ctx, proxyListener, BluetoothProfile.HID_DEVICE);
        if (!ok) { error = "This phone doesn't allow acting as a Bluetooth keyboard."; changed(); }
    }

    public void stop() {
        releaseNow();
        if (hid != null) {
            try { hid.unregisterApp(); } catch (Exception ignored) {}
            if (adapter != null) adapter.closeProfileProxy(BluetoothProfile.HID_DEVICE, hid);
        }
        hid = null; registered = false; host = null;
        connState = BluetoothProfile.STATE_DISCONNECTED;
    }

    private final BluetoothProfile.ServiceListener proxyListener = new BluetoothProfile.ServiceListener() {
        @Override public void onServiceConnected(int profile, BluetoothProfile proxy) {
            hid = (BluetoothHidDevice) proxy;
            BluetoothHidDeviceAppSdpSettings sdp = new BluetoothHidDeviceAppSdpSettings(
                    "Remote Mic", "Remote Mic keyboard", "rm-01",
                    BluetoothHidDevice.SUBCLASS1_KEYBOARD, DESCRIPTOR);
            boolean ok = hid.registerApp(sdp, null, null, io, callback);
            if (!ok) error = "Bluetooth keyboard mode is busy (another keyboard app?).";
            changed();
        }
        @Override public void onServiceDisconnected(int profile) {
            hid = null; registered = false; connState = BluetoothProfile.STATE_DISCONNECTED; changed();
        }
    };

    private final BluetoothHidDevice.Callback callback = new BluetoothHidDevice.Callback() {
        @Override public void onAppStatusChanged(BluetoothDevice plugged, boolean reg) {
            registered = reg;
            if (reg) {
                error = "";
                // Reconnect to the last laptop automatically.
                String last = prefs.getString("host", null);
                if (plugged != null) connect(plugged.getAddress());
                else if (last != null) connect(last);
            }
            changed();
        }
        @Override public void onConnectionStateChanged(BluetoothDevice device, int state) {
            connState = state;
            if (state == BluetoothProfile.STATE_CONNECTED) {
                host = device;
                prefs.edit().putString("host", device.getAddress()).apply();
                error = "";
            } else if (state == BluetoothProfile.STATE_DISCONNECTED && host != null
                    && host.getAddress().equals(device.getAddress())) {
                host = null; holding = false;
            }
            changed();
        }
        @Override public void onGetReport(BluetoothDevice device, byte type, byte id, int bufferSize) {
            if (hid != null) hid.replyReport(device, type, (byte) REPORT_ID, KeyMap.report(0, 0));
        }
    };

    // ------------------------------------------------------------------ keys
    /** Tap: press combo, release. */
    public void tap(String combo) {
        final int[] k = KeyMap.parse(combo);
        io.execute(() -> {
            if (!ready()) return;
            if (k[0] != 0) { send(k[0], 0); sleep(25); } // modifiers first: friendlier to RDP
            send(k[0], k[1]); sleep(45);
            send(k[0], 0); sleep(15);
            send(0, 0);
            lastSent = combo;
            changed();
        });
    }

    /** Push-to-talk: press and keep holding until release(). */
    public void hold(String combo) {
        final int[] k = KeyMap.parse(combo);
        io.execute(() -> {
            if (!ready()) return;
            if (k[0] != 0) { send(k[0], 0); sleep(25); }
            send(k[0], k[1]);
            holding = true;
            lastSent = combo + " (held)";
            changed();
        });
    }

    public void release() {
        io.execute(this::releaseNow);
    }

    private void releaseNow() {
        if (hid != null && host != null && holding) {
            send(0, 0);
            holding = false;
            changed();
        }
    }

    private boolean ready() {
        if (hid == null || host == null || connState != BluetoothProfile.STATE_CONNECTED) {
            error = "Not connected to a laptop.";
            changed();
            return false;
        }
        return true;
    }

    private void send(int mods, int key) {
        boolean ok = hid.sendReport(host, REPORT_ID, KeyMap.report(mods, key));
        if (!ok) error = "Send failed. Reconnect the laptop.";
    }

    private static void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException ignored) {} }

    // ------------------------------------------------------------------ hosts
    public void connect(String address) {
        if (hid == null || adapter == null) return;
        try {
            BluetoothDevice d = adapter.getRemoteDevice(address);
            if (host != null && !host.getAddress().equals(address)) hid.disconnect(host);
            hid.connect(d);
            connState = BluetoothProfile.STATE_CONNECTING;
            changed();
        } catch (Exception e) {
            error = "Can't connect: " + e.getMessage();
            changed();
        }
    }

    public void disconnect() {
        if (hid != null && host != null) hid.disconnect(host);
    }

    // ------------------------------------------------------------------ state for the UI
    public String statusJson() {
        try {
            JSONObject o = new JSONObject();
            o.put("supported", adapter != null);
            o.put("bt_on", adapter != null && adapter.isEnabled());
            o.put("registered", registered);
            o.put("state", connState == BluetoothProfile.STATE_CONNECTED ? "connected"
                    : connState == BluetoothProfile.STATE_CONNECTING ? "connecting" : "disconnected");
            o.put("holding", holding);
            o.put("error", error);
            o.put("last", lastSent);
            o.put("phone", adapter != null ? safeName(adapter.getName()) : "");
            if (host != null) o.put("host", hostJson(host));
            JSONArray hosts = new JSONArray();
            if (adapter != null && adapter.isEnabled()) {
                for (BluetoothDevice d : adapter.getBondedDevices()) {
                    BluetoothClass c = d.getBluetoothClass();
                    boolean computer = c != null && c.getMajorDeviceClass() == BluetoothClass.Device.Major.COMPUTER;
                    JSONObject h = hostJson(d);
                    h.put("computer", computer);
                    if (computer) hosts.put(h);
                }
            }
            o.put("hosts", hosts);
            o.put("preferred", prefs.getString("host", ""));
            return o.toString();
        } catch (Exception e) {
            return "{\"error\":\"" + e.getClass().getSimpleName() + "\"}";
        }
    }

    private JSONObject hostJson(BluetoothDevice d) throws Exception {
        JSONObject h = new JSONObject();
        h.put("name", safeName(d.getName()));
        h.put("address", d.getAddress());
        return h;
    }

    private static String safeName(String n) { return n == null ? "unknown" : n; }

    private void changed() { if (listener != null) listener.onChanged(); }
}
