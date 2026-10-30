package com.lumen.remote;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 本地存储：电脑地址 + 登录令牌。
 *
 * 令牌来自网页里的 localStorage（lumen_tok），由 LumenBridge 在登录后同步过来，
 * 供原生轮询远程请求时使用 —— WebView 的 localStorage 原生读不到。
 */
public final class Prefs {

    private static final String NAME = "lumen";
    private static final String K_HOST = "host";
    private static final String K_TOKEN = "token";
    private static final String K_MODE = "mode";
    private static final String K_RELAY_HOST = "relay_host";
    private static final String K_RELAY_SECRET = "relay_secret";

    private Prefs() {
    }

    private static SharedPreferences sp(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(NAME, Context.MODE_PRIVATE);
    }

    public static String host(Context ctx) {
        return sp(ctx).getString(K_HOST, "");
    }

    public static void setHost(Context ctx, String host) {
        sp(ctx).edit().putString(K_HOST, host == null ? "" : host.trim()).apply();
    }

    public static String token(Context ctx) {
        return sp(ctx).getString(K_TOKEN, "");
    }

    public static void setToken(Context ctx, String token) {
        sp(ctx).edit().putString(K_TOKEN, token == null ? "" : token.trim()).apply();
    }

    public static boolean isConfigured(Context ctx) {
        return !host(ctx).isEmpty();
    }
}
