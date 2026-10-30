package com.lumen.remote;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.RadioGroup;

import android.webkit.CookieManager;
import org.json.JSONObject;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

/**
 * Lumen 手机端。
 *
 * 结构：原生首屏（填电脑地址）→ WebView 加载电脑端下发的手机页面 /m。
 *
 * 为什么不让 APP 自带一份前端：
 *   手机页面和桌面端是同一套代码，桌面端升级了功能，手机端不用重装 APP。
 *   APP 只负责三件原生的事：桌面图标、系统通知、记住连的是哪台电脑。
 */
public class MainActivity extends Activity {

    private WebView web;
    private View setupPanel;
    private View topbar;
    private EditText hostInput;
    private TextView setupHint;
    private TextView topHost;
    private ProgressBar bar;

    private String host = "";
    private String mode = "direct";
    private EditText relayHostInput;
    private EditText relaySecretInput;
    private RadioGroup rgMode;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web);
        setupPanel = findViewById(R.id.setupPanel);
        topbar = findViewById(R.id.topbar);
        hostInput = findViewById(R.id.hostInput);
        setupHint = findViewById(R.id.setupHint);
        topHost = findViewById(R.id.topHost);
        bar = findViewById(R.id.bar);

        rgMode = findViewById(R.id.rgMode);
        relayHostInput = findViewById(R.id.relayHostInput);
        relaySecretInput = findViewById(R.id.relaySecretInput);
        rgMode.setOnCheckedChangeListener((g, id) -> {
            boolean relay = (id == R.id.rbRelay);
            mode = relay ? "relay" : "direct";
            hostInput.setVisibility(relay ? View.GONE : View.VISIBLE);
            relayHostInput.setVisibility(relay ? View.VISIBLE : View.GONE);
            relaySecretInput.setVisibility(relay ? View.VISIBLE : View.GONE);
            setupHint.setText("");
        });

        setupWebView();

        Button btnConnect = findViewById(R.id.btnConnect);
        btnConnect.setOnClickListener(v -> onConnect());
        findViewById(R.id.btnChange).setOnClickListener(v -> showSetup());
        findViewById(R.id.btnReload).setOnClickListener(v -> {
            if (web != null) {
                web.reload();
            }
        });

        askNotificationPermission();

        String savedHost = Prefs.host(this);
        String savedRelay = Prefs.relayHost(this);
        String savedSecret = Prefs.relaySecret(this);
        if ("relay".equals(Prefs.mode(this)) && !savedRelay.isEmpty() && !savedSecret.isEmpty()) {
            mode = "relay";
            rgMode.check(R.id.rbRelay);
            hostInput.setVisibility(View.GONE);
            relayHostInput.setVisibility(View.VISIBLE);
            relaySecretInput.setVisibility(View.VISIBLE);
            relayHostInput.setText(savedRelay);
            relaySecretInput.setText(savedSecret);
            setupHint.setText(R.string.relay_connecting);
            connectRelay(savedRelay, savedSecret);
        } else if (!savedHost.isEmpty()) {
            mode = "direct";
            rgMode.check(R.id.rbDirect);
            hostInput.setText(savedHost);
            loadPage(savedHost);
        } else {
            showSetup();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        // 从通知点进来：如果有地址就回到网页视图
        String h = Prefs.host(this);
        if (!h.isEmpty() && setupPanel.getVisibility() == View.VISIBLE) {
            loadPage(h);
        }
    }

    // ------------------------------------------------------------ WebView

    private void setupWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        // 只走 http(s)，不给 file:// 与 content:// 的口子
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        web.addJavascriptInterface(new LumenBridge(this), "LumenNative");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String h = u.getHost();
                if (h != null && (h.equals(hostOf(host)) || h.equals("127.0.0.1") || h.equals("localhost"))) {
                    return false;
                }
                // 站外链接交给系统浏览器
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                bar.setVisibility(View.GONE);
                // 页面里的 localStorage 在 WebView 里，原生读不到；
                // 让页面主动把令牌同步过来
                view.evaluateJavascript(
                        "(function(){try{var t=localStorage.getItem('lumen_tok')||'';"
                                + "if(t&&window.LumenNative){window.LumenNative.syncToken(t);}}catch(e){}})()",
                        null);
            }
        });
        web.setWebChromeClient(new WebChromeClient());
    }

    private String hostOf(String h) {
        int i = h.indexOf(':');
        return i > 0 ? h.substring(0, i) : h;
    }

    // ------------------------------------------------------------ 交互

    private void onConnect() {
        if ("relay".equals(mode)) {
            String r = relayHostInput.getText().toString().trim();
            String s = relaySecretInput.getText().toString().trim();
            if (r.isEmpty() || s.isEmpty()) {
                setupHint.setText(R.string.relay_err_empty);
                return;
            }
            Prefs.setMode(this, "relay");
            Prefs.setRelayHost(this, r);
            Prefs.setRelaySecret(this, s);
            setupHint.setText(R.string.relay_connecting);
            connectRelay(r, s);
            return;
        }
        String raw = hostInput.getText().toString().trim();
        String norm = normalizeHost(raw);
        if (norm.isEmpty()) {
            setupHint.setText("请填写电脑地址，例如 192.168.1.10:8770");
            return;
        }
        Prefs.setMode(this, "direct");
        Prefs.setHost(this, norm);
        loadPage(norm);
    }

    /** 允许只填 IP：缺端口自动补 8770；允许带 http:// 前缀。 */
    private String normalizeHost(String raw) {
        String s = raw == null ? "" : raw.trim();
        if (s.isEmpty()) {
            return "";
        }
        if (s.startsWith("http://")) {
            s = s.substring(7);
        } else if (s.startsWith("https://")) {
            s = s.substring(8);
        }
        int slash = s.indexOf('/');
        if (slash >= 0) {
            s = s.substring(0, slash);
        }
        if (s.isEmpty()) {
            return "";
        }
        if (s.indexOf(':') < 0) {
            s = s + ":8770";
        }
        return s;
    }

    private void showSetup() {
        setupPanel.setVisibility(View.VISIBLE);
        web.setVisibility(View.GONE);
        topbar.setVisibility(View.GONE);
        boolean relay = "relay".equals(mode);
        hostInput.setVisibility(relay ? View.GONE : View.VISIBLE);
        relayHostInput.setVisibility(relay ? View.VISIBLE : View.GONE);
        relaySecretInput.setVisibility(relay ? View.VISIBLE : View.GONE);
        if (relay) {
            relayHostInput.setText(Prefs.relayHost(this));
            relaySecretInput.setText(Prefs.relaySecret(this));
        } else {
            hostInput.setText(Prefs.host(this));
        }
        setupHint.setText("");
    }

    private void loadPage(String h) {
        host = h;
        setupPanel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        topbar.setVisibility(View.VISIBLE);
        topHost.setText(h);
        bar.setVisibility(View.VISIBLE);
        web.loadUrl("http://" + h + "/m");
        startWatcher();
    }

    // ---------------------------------------------------------- 跨网中继

    private void connectRelay(String base, String secret) {
        if (!base.startsWith("http://") && !base.startsWith("https://")) {
            base = "https://" + base;
        }
        final String relayBase = base.replaceAll("/+$", "");
        new Thread(() -> {
            try {
                URL u = new URL(relayBase + "/relay/connect");
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
                c.setConnectTimeout(15000);
                c.setReadTimeout(20000);
                String body = "k=" + URLEncoder.encode(secret, "UTF-8");
                c.getOutputStream().write(body.getBytes("UTF-8"));
                int code = c.getResponseCode();
                InputStream in = (code == 200) ? c.getInputStream() : c.getErrorStream();
                String txt = new String(readAll(in), "UTF-8");
                if (code != 200) {
                    throw new Exception("连接失败(" + code + ")");
                }
                JSONObject o = new JSONObject(txt);
                if (!o.optBoolean("ok", false)) {
                    throw new Exception(o.optString("error", "密钥错误"));
                }
                String sid = o.optString("sid", "");
                if (sid.isEmpty()) {
                    throw new Exception("未返回会话标识");
                }
                CookieManager cm = CookieManager.getInstance();
                cm.setAcceptCookie(true);
                cm.setCookie(relayBase, "lrsid=" + sid);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    cm.flush();
                }
                final String url = relayBase + "/m";
                runOnUiThread(() -> loadRelayPage(relayBase, url));
            } catch (Exception e) {
                final String msg = e.getMessage();
                runOnUiThread(() -> setupHint.setText("中继连接失败：" + (msg == null ? "未知错误" : msg)));
            }
        }).start();
    }

    private byte[] readAll(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    private void loadRelayPage(String relayBase, String url) {
        Uri uri = Uri.parse(relayBase);
        host = uri.getHost() != null ? uri.getHost() : relayBase;
        setupPanel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        topbar.setVisibility(View.VISIBLE);
        topHost.setText(relayBase);
        bar.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    // ------------------------------------------------------------ 原生桥

    String currentHost() {
        return host;
    }

    void onTokenFromPage(String token) {
        if (token == null || token.isEmpty()) {
            return;
        }
        Prefs.setToken(this, token);
        startWatcher();
    }

    void showNotice(String title, String body) {
        NotifyService.push(this, title, body);
    }

    /** 令牌到手后才启动轮询服务，否则没有鉴权信息。 */
    private void startWatcher() {
        if (Prefs.token(this).isEmpty()) {
            return;
        }
        try {
            Intent i = new Intent(this, NotifyService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(i);
            } else {
                startService(i);
            }
        } catch (Exception e) {
            Toast.makeText(this, "通知服务启动失败：" + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return;
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1001);
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }
}
