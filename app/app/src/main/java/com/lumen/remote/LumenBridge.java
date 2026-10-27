package com.lumen.remote;

import android.webkit.JavascriptInterface;

/**
 * 暴露给网页的原生接口。
 *
 * 网页（桌面端下发的 mobile.html）登录成功后调用：
 *     LumenNative.syncToken(token)
 * 原生拿到令牌后就能在后台轮询远程控制请求并发系统通知。
 */
public class LumenBridge {

    private final MainActivity act;

    LumenBridge(MainActivity act) {
        this.act = act;
    }

    @JavascriptInterface
    public void syncToken(String token) {
        act.onTokenFromPage(token);
    }

    @JavascriptInterface
    public String getHost() {
        return act.currentHost();
    }

    @JavascriptInterface
    public void notify(String title, String body) {
        act.showNotice(title, body);
    }
}
