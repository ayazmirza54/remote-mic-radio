package com.ayazmirza.remotemic;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.net.Uri;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.view.View;
import android.view.WindowManager;
import android.graphics.Bitmap;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebViewClient;
import android.webkit.WebSettings;
import android.webkit.WebView;

import java.util.ArrayList;
import java.util.List;

/** Hosts the walkie-talkie UI (assets/index.html) and bridges its buttons to the Bluetooth keyboard. */
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 7;
    private static final int REQ_ENABLE_BT = 8;

    private static final String HOME = "file:///android_asset/index.html";

    private WebView web;
    private HidKeyboard kb;
    private SharedPreferences prefs;
    // True only while our own Bluetooth page is shown: the laptop's page may not drive the BT keyboard.
    private volatile boolean onHome = true;

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
        prefs = getSharedPreferences("rm", MODE_PRIVATE);
        kb = new HidKeyboard(this, this::pushState);
        web.addJavascriptInterface(new Bridge(), "RM");
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageStarted(WebView v, String url, Bitmap icon) { onHome = url.startsWith(HOME); }

            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri a = Uri.parse(prefs.getString("agent", ""));
                return !"file".equals(r.getUrl().getScheme()) && !String.valueOf(a.getAuthority()).equals(r.getUrl().getAuthority());
            }

            @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                // Laptop agent unreachable: fall back to the Bluetooth page, which says so.
                if (r.isForMainFrame() && !"file".equals(r.getUrl().getScheme())) v.loadUrl(HOME + "#wifi-down");
            }
        });
        if (!handleLink(getIntent())) loadHome();
        setContentView(web);

        registerReceiver(btReceiver, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED));
        ensurePermissionsThenStart();
    }

    private void loadHome() {
        String agent = prefs.getString("agent", null);
        web.loadUrl("wifi".equals(prefs.getString("mode", "")) && agent != null ? agent : HOME);
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        setIntent(i);
        handleLink(i);
    }

    /** remotemic://connect?u=http://192.168.1.5:8765/?t=TOKEN, sent by the agent's page in the phone browser. */
    private boolean handleLink(Intent i) {
        Uri d = i == null ? null : i.getData();
        if (d == null || !"remotemic".equals(d.getScheme())) return false;
        return useWifi(d.getQueryParameter("u")) == null;
    }

    static boolean validAgent(String u) {
        if (u == null) return false;
        Uri a = Uri.parse(u.trim());
        return ("http".equals(a.getScheme()) || "https".equals(a.getScheme()))
                && a.getHost() != null && !a.getHost().isEmpty()
                && a.getQueryParameter("t") != null && !a.getQueryParameter("t").isEmpty();
    }

    /** Switch to Wi-Fi mode. Returns null on success, else an error for the user. */
    private String useWifi(String url) {
        if (!validAgent(url)) return "That isn't a Remote Mic link. Copy the whole link from RemoteMic.exe (it ends with ?t=…).";
        String u = url.trim();
        prefs.edit().putString("agent", u).putString("mode", "wifi").apply();
        kb.release();
        runOnUiThread(() -> web.loadUrl(u));
        return null;
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
        @JavascriptInterface public String tap(String combo) { return onHome ? guard(() -> kb.tap(combo)) : "not allowed"; }
        @JavascriptInterface public String hold(String combo) { return onHome ? guard(() -> kb.hold(combo)) : "not allowed"; }
        @JavascriptInterface public void release() { kb.release(); }
        @JavascriptInterface public void connect(String address) { if (onHome) kb.connect(address); }

        /** Wi-Fi mode: the saved laptop agent link ("" if none). */
        @JavascriptInterface public String agent() { return prefs.getString("agent", ""); }
        @JavascriptInterface public String useWifi(String url) { String e = MainActivity.this.useWifi(url); return e == null ? "ok" : e; }
        @JavascriptInterface public void useBluetooth() {
            prefs.edit().putString("mode", "bt").apply();
            runOnUiThread(() -> web.loadUrl(HOME));
        }
        @JavascriptInterface public void disconnect() { kb.disconnect(); }
        @JavascriptInterface public void retry() { runOnUiThread(MainActivity.this::ensurePermissionsThenStart); }

        @JavascriptInterface public void discoverable() {
            if (!onHome) return;
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
