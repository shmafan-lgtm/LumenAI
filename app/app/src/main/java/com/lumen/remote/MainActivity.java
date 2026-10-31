package com.lumen.remote;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.net.http.SslError;
import android.webkit.SslErrorHandler;
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
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;

import android.webkit.CookieManager;
import org.json.JSONObject;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

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
    private Handler relayRetryHandler;
    private SSLSocketFactory relaySsl;   // 额外信任 Let's Encrypt 根证书的 SSL 工厂
    private String lastRelayError = "";   // 最近一次失败原因，直接显示给用户看

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

        // 深链接优先：桌面端二维码扫进来时，直接带上地址与密钥自动连上。
        if (handleDeepLink(getIntent())) {
            return;
        }

        String savedHost = Prefs.host(this);
        String savedRelay = Prefs.relayHost(this);
        String savedSecret = Prefs.relaySecret(this);
        if ("relay".equals(Prefs.mode(this)) && !savedSecret.isEmpty()) {
            mode = "relay";
            rgMode.check(R.id.rbRelay);
            hostInput.setVisibility(View.GONE);
            relayHostInput.setVisibility(View.VISIBLE);
            relaySecretInput.setVisibility(View.VISIBLE);
            relaySecretInput.setText(savedSecret);
            // 不再直接用可能已过期的 savedRelay：启动即先从地址簿拉「当前活着」的
            // 地址自动连，并把正确地址填进输入框，避免手填 60 字符长地址出错。
            autoBootstrapConnect(savedSecret);
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
        if (handleDeepLink(intent)) {
            return;
        }
        // 从通知点进来：如果有地址就回到网页视图
        String h = Prefs.host(this);
        if (!h.isEmpty() && setupPanel.getVisibility() == View.VISIBLE) {
            loadPage(h);
        }
    }

    /**
     * 处理深链接 lumentunnel://relay?addr=<地址>&k=<密钥>。
     *
     * 这是公版的连接方式：桌面端「手机」面板把地址与密钥编码进二维码，
     * 手机相机一扫就唤起本 APP 并自动填好、连上。
     * 因此公版安装包里**不需要**内置任何人的地址簿（那会把别人的电脑暴露出去）。
     *
     * @return 是否消费了一个深链接
     */
    private boolean handleDeepLink(Intent intent) {
        if (intent == null) return false;
        Uri u;
        try {
            u = intent.getData();
        } catch (Exception e) {
            return false;
        }
        if (u == null) return false;
        if (!"lumentunnel".equalsIgnoreCase(u.getScheme())) return false;
        String addr = u.getQueryParameter("addr");
        String k = u.getQueryParameter("k");
        if (addr == null || addr.trim().isEmpty()) return false;
        addr = addr.trim();
        mode = "relay";
        rgMode.check(R.id.rbRelay);
        hostInput.setVisibility(View.GONE);
        relayHostInput.setVisibility(View.VISIBLE);
        relaySecretInput.setVisibility(View.VISIBLE);
        relayHostInput.setText(addr);
        if (k != null && !k.trim().isEmpty()) {
            relaySecretInput.setText(k.trim());
        }
        setupHint.setText("已从二维码读取连接信息，正在连接…");
        // 复用正常连接流程（保存 + 拨号 + 自愈重试）
        onConnect();
        return true;
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

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // 老 Android（API 23 这类）的系统证书库没有 Let's Encrypt 的根证书，
                // 我们的中继域名会验不过，页面直接打不开。
                // 只对「我们自己那个中继域名」放行，其它域名一律照常拒绝，
                // 不给中间人留口子（凭据只发给中继，不发给别的站）。
                String h = "";
                try {
                    Uri u = Uri.parse(error.getUrl());
                    if (u.getHost() != null) h = hostOf(u.getHost());
                } catch (Exception ignored) {
                }
                if (isRelayHost(h)) {
                    handler.proceed();
                } else {
                    handler.cancel();
                }
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
            if (s.isEmpty()) {
                setupHint.setText(R.string.relay_err_empty);
                return;
            }
            Prefs.setMode(this, "relay");
            Prefs.setRelaySecret(this, s);
            setupHint.setText(R.string.relay_connecting);
            if (!r.isEmpty()) {
                Prefs.setRelayHost(this, r);
                connectRelay(r, s);
            } else {
                // 地址框为空：不去逼用户手填长串，直接去地址簿拉最新地址自动连
                autoBootstrapConnect(s);
            }
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
        // 候选地址：当前填的优先，再把历史地址挨个试一遍。
        // 免费隧道每 60 分钟换一次公网地址，旧的会失效，
        // 留着历史地址就能在隧道自愈后自动接上最新那条。
        java.util.List<String> cands = new java.util.ArrayList<>();
        String cur = normRelay(base);
        if (!cur.isEmpty()) cands.add(cur);
        String known = Prefs.relayHosts(this);
        if (!known.isEmpty()) {
            for (String h : known.split(",")) {
                String n = normRelay(h);
                if (!n.isEmpty() && !cands.contains(n)) cands.add(n);
            }
        }
        tryConnectRelay(cands, 0, secret, false);
    }

    private String normRelay(String base) {
        if (base == null) return "";
        String b = base.trim();
        if (b.isEmpty()) return "";
        if (!b.startsWith("http://") && !b.startsWith("https://")) b = "https://" + b;
        return b.replaceAll("/+$", "");
    }

    /**
     * https 降级成明文 http（同一个域名）。
     *
     * 为什么需要：免费隧道服务（pinggy 等）现在用的是 Let's Encrypt 的新证书链
     * （YR1 → Root YR，由 Root X1 交叉签名）。老版本 Android（例如 API 23）
     * 出厂证书库里根本没有 Root X1，TLS 握手直接失败 —— 表现就是「连不上」，
     * 但服务端一切正常，非常难查。
     *
     * 隧道本身（电脑 ↔ 隧道服务）是 SSH 加密的，只有手机到隧道服务这一段是明文；
     * 实在验不过证书时，这是「能连上」和「完全连不上」之间的取舍。
     */
    private String toPlain(String base) {
        if (base == null) return "";
        String b = base.trim();
        if (b.startsWith("https://")) return "http://" + b.substring(8);
        return b;
    }

    /**
     * 带 Let's Encrypt 根证书的 SSL 工厂。
     *
     * 系统信任库（老 Android 缺 Root X1）+ 我们自带的 Root X1 合并成一个信任库，
     * 这样 https 依然可用、流量照常加密，不需要降级到明文。
     */
    private SSLSocketFactory relaySslFactory() {
        if (relaySsl != null) return relaySsl;
        try {
            KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
            ks.load(null, null);
            // 1) 先把系统信任库里的根证书全搬进来
            TrustManagerFactory sys = TrustManagerFactory.getInstance(
                    TrustManagerFactory.getDefaultAlgorithm());
            sys.init((KeyStore) null);
            for (TrustManager tm : sys.getTrustManagers()) {
                if (tm instanceof X509TrustManager) {
                    Certificate[] issuers = ((X509TrustManager) tm).getAcceptedIssuers();
                    for (Certificate c : issuers) {
                        try {
                            ks.setCertificateEntry(c.getPublicKey().toString(), c);
                        } catch (Exception ignored) {
                        }
                    }
                }
            }
            // 2) 再加上 Let's Encrypt 的根（系统里可能没有）
            try (InputStream in = getResources().openRawResource(R.raw.isrg_root_x1)) {
                CertificateFactory cf = CertificateFactory.getInstance("X.509");
                Certificate le = cf.generateCertificate(in);
                ks.setCertificateEntry("isrg-root-x1", le);
            } catch (Exception ignored) {
            }
            TrustManagerFactory tmf = TrustManagerFactory.getInstance(
                    TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(ks);
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, tmf.getTrustManagers(), null);
            relaySsl = ctx.getSocketFactory();
        } catch (Exception e) {
            relaySsl = null;
        }
        return relaySsl;
    }

    /** 把异常翻译成人话，直接显示在界面上，别再只报「连不上」。 */
    private String describeRelayError(Exception e) {
        if (e == null) return "未知错误";
        String simple = e.getClass().getSimpleName();
        if (e instanceof java.net.UnknownHostException) {
            return "域名解析失败（DNS 解析不到这个地址）";
        }
        if (e instanceof javax.net.ssl.SSLException) {
            return "HTTPS 证书校验失败（本机不信任该证书）：" + simple;
        }
        if (e instanceof java.net.SocketTimeoutException) {
            return "连接超时（网络不通或隧道已失效）";
        }
        if (e instanceof java.net.ConnectException) {
            return "连接被拒绝（隧道可能已换址）";
        }
        String msg = e.getMessage();
        if (msg != null && msg.contains("Canceled")) {
            return "连接被中断";
        }
        return simple + (msg == null || msg.isEmpty() ? "" : "：" + msg);
    }

    /** 这个域名是不是我们自己的中继域名（放宽 WebView 证书校验时只认它）。 */
    private boolean isRelayHost(String h) {
        if (h == null || h.isEmpty()) return false;
        if (host != null && h.equals(hostOf(host))) return true;
        String saved = Prefs.relayHost(this);
        if (saved != null && !saved.isEmpty()) {
            try {
                String sh = Uri.parse(saved).getHost();
                if (sh != null && h.equals(hostOf(sh))) return true;
            } catch (Exception ignored) {
            }
        }
        String many = Prefs.relayHosts(this);
        if (many != null && !many.isEmpty()) {
            for (String s : many.split(",")) {
                try {
                    String sh = Uri.parse(s.trim()).getHost();
                    if (sh != null && h.equals(hostOf(sh))) return true;
                } catch (Exception ignored) {
                }
            }
        }
        return false;
    }

    private void tryConnectRelay(final java.util.List<String> cands, final int idx,
                                 final String secret, final boolean fromBootstrap) {
        tryConnectRelay(cands, idx, secret, fromBootstrap, false);
    }

    /**
     * @param plain true = 这条地址已经降级成明文 http 再试一次
     *              （老 Android 证书库缺 Let's Encrypt 的根，https 握手会直接失败）
     */
    private void tryConnectRelay(final java.util.List<String> cands, final int idx,
                                 final String secret, final boolean fromBootstrap,
                                 final boolean plain) {
        if (idx >= cands.size()) {
            if (!fromBootstrap) {
                fetchBootstrapAndRetry(secret);
                return;
            }
            final String why = lastRelayError.isEmpty() ? "" : ("：" + lastRelayError);
            runOnUiThread(() -> setupHint.setText("中继连接失败" + why + "，稍后自动重试"));
            scheduleRelayRetry(secret);
            return;
        }
        final String base = cands.get(idx);
        final String relayBase = plain ? toPlain(base) : base;
        new Thread(() -> {
            try {
                URL u = new URL(relayBase + "/relay/connect");
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                if (c instanceof HttpsURLConnection) {
                    SSLSocketFactory f = relaySslFactory();
                    if (f != null) ((HttpsURLConnection) c).setSSLSocketFactory(f);
                }
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
                    throw new Exception("连接失败(HTTP " + code + ")");
                }
                JSONObject o = new JSONObject(txt);
                if (!o.optBoolean("ok", false)) {
                    throw new Exception(o.optString("error", "密钥错误"));
                }
                String sid = o.optString("sid", "");
                if (sid.isEmpty()) {
                    throw new Exception("未返回会话标识");
                }
                lastRelayError = "";
                // setCookie() 是异步的：以前写完立刻 loadUrl，WebView 请求 /m 时
                // cookie 还没落盘，中继会回 403「未连接中继」。
                // 现在等 setCookie 的回调回来再加载页面。
                // URL 上同时带中继密钥与登录令牌：换地址后靠这一手自动续上登录态，
                // 落地页会自动 connect，页面会自动登录，全程不用重新输账号。
                String tok = Prefs.token(this);
                String next = "/m" + (tok == null || tok.isEmpty() ? "" : "?t=" + URLEncoder.encode(tok, "UTF-8"));
                final String url = relayBase + "/?k=" + URLEncoder.encode(secret, "UTF-8")
                        + "&next=" + URLEncoder.encode(next, "UTF-8");
                final String cookieVal = "lrsid=" + sid + "; Path=/";
                // ⚠️ CookieManager.setCookie(url, value, callback) 只能在「有 Looper 的线程」上调用。
                // 这段代码跑在后台 new Thread 里 → 抛 IllegalStateException:
                // "SetCookie must be called on a thread with a running Looper"。
                // 而这个异常以前被下面的 catch 吃掉、统一显示成「已知地址都连不上」，
                // 于是地址过期、证书不被信任、网络不通……全被掩盖了，
                // 排查时一直在查错误的方向。所以整块搬到主线程（主线程有 Looper）。
                runOnUiThread(() -> {
                    CookieManager cm = CookieManager.getInstance();
                    cm.setAcceptCookie(true);
                    cm.setCookie(relayBase, cookieVal, value -> {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                            cm.flush();
                        }
                        if (relayRetryHandler != null) relayRetryHandler.removeCallbacksAndMessages(null);
                        loadRelayPage(relayBase, url);
                    });
                });
                Prefs.setRelayHost(this, relayBase);
                // 中继这次回传了所有当前可用地址：全部缓存下来。
                // 之后隧道轮换，APP 直接拿这份最新列表去试，无需再依赖 GitHub（国内常被墙）。
                org.json.JSONArray addrArr = o.optJSONArray("addresses");
                if (addrArr != null && addrArr.length() > 0) {
                    java.util.List<String> fresh = new java.util.ArrayList<>();
                    for (int i = 0; i < addrArr.length(); i++) {
                        String a = addrArr.optString(i, "");
                        String n = normRelay(a);
                        if (!n.isEmpty() && !fresh.contains(n)) fresh.add(n);
                    }
                    if (fresh.size() > 0) {
                        Prefs.setRelayHosts(MainActivity.this, TextUtils.join(",", fresh));
                    }
                }
            } catch (Exception e) {
                lastRelayError = describeRelayError(e);
                if (!plain) {
                    // 这条地址的 https 握手失败（多半是本机不信任该证书）→
                    // 同一个地址降级成明文 http 再试一次，成功就用明文继续。
                    tryConnectRelay(cands, idx, secret, fromBootstrap, true);
                    return;
                }
                // 这条地址不行（多半是隧道刚换过），换下一条候选
                tryConnectRelay(cands, idx + 1, secret, fromBootstrap, false);
            }
        }).start();
    }

    /**
     * 从「稳定的地址中枢」(GitHub Gist，raw URL 在 strings.xml 的 relay_bootstrap_url) 拉最新隧道地址。
     * 这是一把固定钥匙：地址中枢的 URL 永不变，里面放的是会变的隧道地址。
     * 手机连不上任何已知地址时来这里取，就能在隧道轮换/换网络后自动接上。
     */
    private void fetchBootstrapAndRetry(final String secret) {
        String bootRaw;
        try {
            bootRaw = getString(R.string.relay_bootstrap_url);
        } catch (Exception ignored) {
            bootRaw = "";
        }
        final String boot = (bootRaw == null) ? "" : bootRaw;
        if (boot == null || boot.trim().isEmpty()) {
            runOnUiThread(() -> setupHint.setText("中继连接失败：已知地址都连不上，稍后自动重试"));
            scheduleRelayRetry(secret);
            return;
        }
        new Thread(() -> {
            java.util.List<String> extra = new java.util.ArrayList<>();
            try {
                URL u = new URL(boot.trim());
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("GET");
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                if (c.getResponseCode() != 200) throw new Exception("中枢不可达");
                String t = new String(readAll(c.getInputStream()), "UTF-8");
                org.json.JSONArray arr = null;
                try {
                    org.json.JSONObject o = new JSONObject(t);
                    arr = o.optJSONArray("addresses");
                } catch (Exception e) {
                    arr = null;
                }
                if (arr == null) {
                    try {
                        arr = new org.json.JSONArray(t);
                    } catch (Exception e2) {
                        arr = null;
                    }
                }
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        String a = arr.optString(i, "");
                        if (a.isEmpty() && arr.optJSONObject(i) != null) {
                            a = arr.getJSONObject(i).optString("url", "");
                        }
                        String n = normRelay(a);
                        if (!n.isEmpty() && !extra.contains(n)) extra.add(n);
                    }
                }
            } catch (Exception e) {
                // 中枢也取不到，稍后重试
            }
            if (extra.size() > 0) {
                Prefs.setRelayHosts(this, TextUtils.join(",", extra));
            }
            // 合并：当前填的 + 中枢新地址 + 历史地址
            java.util.List<String> merged = new java.util.ArrayList<>();
            String cur = normRelay(Prefs.relayHost(this));
            if (!cur.isEmpty() && !merged.contains(cur)) merged.add(cur);
            for (String h : extra) if (!merged.contains(h)) merged.add(h);
            String known = Prefs.relayHosts(this);
            if (!known.isEmpty()) {
                for (String h : known.split(",")) {
                    String n = normRelay(h);
                    if (!n.isEmpty() && !merged.contains(n)) merged.add(n);
                }
            }
            if (merged.size() > 0) {
                tryConnectRelay(merged, 0, secret, true);
            } else {
                runOnUiThread(() -> setupHint.setText("中继连接失败"
                        + (lastRelayError.isEmpty() ? "" : ("：" + lastRelayError))
                        + "，稍后自动重试"));
                scheduleRelayRetry(secret);
            }
        }).start();
    }

    /**
     * 启动/进入中继模式时：先去「稳定的地址中枢」(GitHub Gist) 拉当前活着的隧道地址，
     * 用最新地址自动连，并把正确地址填进输入框（用户看得见、也不用自己手填长串）。
     * 这样彻底绕开「手填 60 字符长地址被截断/粘错」这一整类问题 —— 用户只需填过一次密钥。
     */
    private void autoBootstrapConnect(final String secret) {
        String bootRaw;
        try {
            bootRaw = getString(R.string.relay_bootstrap_url);
        } catch (Exception ignored) {
            bootRaw = "";
        }
        final String boot = (bootRaw == null) ? "" : bootRaw.trim();
        if (boot.isEmpty()) {
            // 没配置地址中枢：退回用已存地址手动连
            String r = Prefs.relayHost(this);
            if (!r.isEmpty()) {
                relayHostInput.setText(r);
                setupHint.setText(R.string.relay_connecting);
                connectRelay(r, secret);
            } else {
                showSetup();
            }
            return;
        }
        setupHint.setText("正在从地址簿获取最新中继地址…");
        new Thread(() -> {
            java.util.List<String> addrs = new java.util.ArrayList<>();
            try {
                URL u = new URL(boot);
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("GET");
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                if (c.getResponseCode() == 200) {
                    String t = new String(readAll(c.getInputStream()), "UTF-8");
                    addrs = parseBootstrapAddresses(t);
                }
            } catch (Exception ignored) {
            }
            // 兜底：把「已缓存的多地址列表」和「上次单地址」都加进来。
            // 万一 GitHub 在国内被墙、Gist 取不到，也能用之前成功连上的最新地址自动连。
            String hosts = Prefs.relayHosts(this);
            if (!hosts.isEmpty()) {
                for (String h : hosts.split(",")) {
                    String n = normRelay(h);
                    if (!n.isEmpty() && !addrs.contains(n)) addrs.add(n);
                }
            }
            String saved = Prefs.relayHost(this);
            if (!saved.isEmpty()) {
                String n = normRelay(saved);
                if (!n.isEmpty() && !addrs.contains(n)) addrs.add(n);
            }
            if (addrs.isEmpty()) {
                runOnUiThread(() -> setupHint.setText("无法获取中继地址，请检查网络或手动填写地址"));
                return;
            }
            final java.util.List<String> all = new java.util.ArrayList<>(addrs);
            runOnUiThread(() -> {
                // 把最新地址填进输入框，用户能直接看到它是对的
                relayHostInput.setText(all.isEmpty() ? "" : all.get(0));
                setupHint.setText("已获取最新地址，正在连接…");
                // 直接拿整份候选列表去试：Gist 取到的 + 之前缓存的最新地址都覆盖到
                tryConnectRelay(all, 0, secret, true);
            });
        }).start();
    }

    /** 解析地址中枢返回的 JSON：支持 {"addresses":[...]} 或纯数组。 */
    private java.util.List<String> parseBootstrapAddresses(String t) {
        java.util.List<String> out = new java.util.ArrayList<>();
        if (t == null || t.trim().isEmpty()) return out;
        org.json.JSONArray arr = null;
        try {
            org.json.JSONObject o = new JSONObject(t);
            arr = o.optJSONArray("addresses");
        } catch (Exception e) {
            arr = null;
        }
        if (arr == null) {
            try {
                arr = new org.json.JSONArray(t);
            } catch (Exception e2) {
                arr = null;
            }
        }
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                String a = arr.optString(i, "");
                if (a.isEmpty()) {
                    try {
                        if (arr.optJSONObject(i) != null) {
                            a = arr.getJSONObject(i).optString("url", "");
                        }
                    } catch (Exception e) {
                        a = "";
                    }
                }
                String n = normRelay(a);
                if (!n.isEmpty() && !out.contains(n)) out.add(n);
            }
        }
        return out;
    }

    /** 连接失败后每 30 秒自动重试；隧道自愈（换上新地址）后就能自动连上，无需手动点。 */
    private void scheduleRelayRetry(final String secret) {
        if (relayRetryHandler == null) {
            relayRetryHandler = new Handler(Looper.getMainLooper());
        } else {
            relayRetryHandler.removeCallbacksAndMessages(null);
        }
        relayRetryHandler.postDelayed(() -> {
            if ("relay".equals(mode)) {
                String r = Prefs.relayHost(this);
                String k = Prefs.relaySecret(this);
                if (!r.isEmpty() && !k.isEmpty()) {
                    setupHint.setText("正在重试连接中继…");
                    connectRelay(r, k);
                }
            }
        }, 30000);
    }

    /**
     * 问中继要当前所有可用隧道地址，存下来备用。
     * 隧道换址后，旧地址失效但列表里有新的，启动时就能自动接上。
     */
    private void refreshRelayHosts(final String relayBase, final String secret) {
        new Thread(() -> {
            try {
                URL u = new URL(relayBase + "/relay/addresses?k=" + URLEncoder.encode(secret, "UTF-8"));
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("GET");
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                if (c.getResponseCode() != 200) return;
                String t = new String(readAll(c.getInputStream()), "UTF-8");
                org.json.JSONArray arr = new JSONObject(t).optJSONArray("addresses");
                if (arr == null) return;
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < arr.length(); i++) {
                    String u2 = arr.optJSONObject(i) == null ? "" : arr.getJSONObject(i).optString("url", "");
                    if (!u2.isEmpty()) {
                        if (sb.length() > 0) sb.append(",");
                        sb.append(u2);
                    }
                }
                if (sb.length() > 0) Prefs.setRelayHosts(this, sb.toString());
            } catch (Exception ignored) {
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
