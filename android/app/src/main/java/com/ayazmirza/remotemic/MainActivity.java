package com.ayazmirza.remotemic;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import java.util.ArrayList;
import java.util.List;

/** Hosts the walkie-talkie UI (assets/index.html) and bridges its buttons to the Bluetooth keyboard. */
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 7;
    private static final int REQ_ENABLE_BT = 8;

    private WebView web;
    private HidKeyboard kb;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(0xFFE6E5E1);
        getWindow().setNavigationBarColor(0xFFE6E5E1);
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);

        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(true);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setBackgroundColor(0xFFE6E5E1);
        kb = new HidKeyboard(this, this::pushState);
        web.addJavascriptInterface(new Bridge(), "RM");
        web.loadUrl("file:///android_asset/index.html");
        setContentView(web);

        registerReceiver(btReceiver, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED));
        ensurePermissionsThenStart();
    }

    private void ensurePermissionsThenStart() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            List<String> need = new ArrayList<>();
            for (String p : new String[]{Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.BLUETOOTH_ADVERTISE}) {
                if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) need.add(p);
            }
            if (!need.isEmpty()) {
                requestPermissions(need.toArray(new String[0]), REQ_PERMS);
                return;
            }
        }
        BluetoothAdapter a = BluetoothAdapter.getDefaultAdapter();
        if (a != null && !a.isEnabled()) {
            //noinspection MissingPermission
            startActivityForResult(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE), REQ_ENABLE_BT);
            return;
        }
        kb.start();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        for (int r : results) {
            if (r != PackageManager.PERMISSION_GRANTED) {
                pushState();
                return;
            }
        }
        ensurePermissionsThenStart();
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (code == REQ_ENABLE_BT) kb.start();
    }

    private final BroadcastReceiver btReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            int s = i.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1);
            if (s == BluetoothAdapter.STATE_ON) kb.start();
            if (s == BluetoothAdapter.STATE_OFF) kb.stop();
            pushState();
        }
    };

    /** Safety: leaving the app always lets go of any held push-to-talk key. */
    @Override
    protected void onPause() {
        super.onPause();
        kb.release();
    }

    @Override
    protected void onDestroy() {
        try { unregisterReceiver(btReceiver); } catch (Exception ignored) {}
        kb.stop();
        web.destroy();
        super.onDestroy();
    }

    private void pushState() {
        if (web != null) web.post(() -> web.evaluateJavascript("window.onNative && window.onNative()", null));
    }

    /** Everything the page can call as window.RM.* */
    private class Bridge {
        @JavascriptInterface public String status() { return kb.statusJson(); }
        @JavascriptInterface public String tap(String combo) { return guard(() -> kb.tap(combo)); }
        @JavascriptInterface public String hold(String combo) { return guard(() -> kb.hold(combo)); }
        @JavascriptInterface public void release() { kb.release(); }
        @JavascriptInterface public void connect(String address) { kb.connect(address); }
        @JavascriptInterface public void disconnect() { kb.disconnect(); }
        @JavascriptInterface public void retry() { runOnUiThread(MainActivity.this::ensurePermissionsThenStart); }

        @JavascriptInterface public void discoverable() {
            runOnUiThread(() -> {
                Intent i = new Intent(BluetoothAdapter.ACTION_REQUEST_DISCOVERABLE);
                i.putExtra(BluetoothAdapter.EXTRA_DISCOVERABLE_DURATION, 120);
                //noinspection MissingPermission
                startActivity(i);
            });
        }

        @JavascriptInterface public void openBluetoothSettings() {
            runOnUiThread(() -> startActivity(new Intent(android.provider.Settings.ACTION_BLUETOOTH_SETTINGS)));
        }

        @JavascriptInterface public void vibrate(int ms) {
            Vibrator v = (Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) v.vibrate(VibrationEffect.createOneShot(Math.max(1, ms), VibrationEffect.DEFAULT_AMPLITUDE));
        }

        private String guard(Runnable r) {
            try { r.run(); return "ok"; } catch (IllegalArgumentException e) { return e.getMessage(); }
        }
    }
}
