package com.lumen.remote;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * 前台服务：持续轮询电脑端的远控状态。
 *
 * 桌面端有人发起远程控制请求时，手机收到系统通知（不需要 APP 在前台）。
 * 这是「装到桌面、有图标、能推送」的落地方式：原生通知 + 后台轮询。
 */
public class NotifyService extends Service {

    private static final String TAG = "LumenNotify";
    private static final String CH_ID = "lumen_remote";
    private static final int NOTI_FOREGROUND = 1001;
    private static final int NOTI_REQUEST = 1002;
    private static final int NOTI_GENERAL = 1003;

    private ScheduledExecutorService pool;
    private String lastPendingId = "";
    private boolean sessionNotified = false;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        startForeground(NOTI_FOREGROUND, foregroundNotification());
        pool = Executors.newSingleThreadScheduledExecutor();
        pool.scheduleWithFixedDelay(this::poll, 2, 4, TimeUnit.SECONDS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && intent.getAction() != null
                && intent.getAction().equals("com.lumen.remote.STOP")) {
            stopSelf();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (pool != null) {
            pool.shutdownNow();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ------------------------------------------------------------ 轮询

    private void poll() {
        try {
            String host = Prefs.host(this);
            String token = Prefs.token(this);
            if (host.isEmpty() || token.isEmpty()) {
                return;
            }
            String body = get("http://" + host + "/api/remote/status", token);
            if (body == null) {
                return;
            }
            // 极简解析：只关心 pending.id 与 session 的有无
            String pendingId = pick(body, "\"pending\"");
            if (pendingId != null && !pendingId.isEmpty() && !pendingId.equals(lastPendingId)) {
                lastPendingId = pendingId;
                notifyOnce(NOTI_REQUEST,
                        "Lumen · 远程控制请求",
                        "有人请求控制这台电脑，点此查看",
                        "request");
            }
            boolean hasSession = body.contains("\"session\":{");
            if (hasSession && !sessionNotified) {
                sessionNotified = true;
                notifyOnce(NOTI_GENERAL,
                        "Lumen · 远程控制已连接",
                        "你现在可以操作这台电脑了",
                        "session");
            } else if (!hasSession) {
                sessionNotified = false;
            }
        } catch (Throwable t) {
            Log.w(TAG, "poll failed: " + t);
        }
    }

    /** 从 JSON 文本里取出 pending 对象的 id（避免引入 JSON 库）。 */
    private static String pick(String body, String key) {
        int i = body.indexOf(key);
        if (i < 0) {
            return "";
        }
        int at = body.indexOf("\"id\"", i);
        if (at < 0) {
            return "";
        }
        int q1 = body.indexOf('"', body.indexOf(':', at) + 1);
        int q2 = body.indexOf('"', q1 + 1);
        if (q1 < 0 || q2 < 0) {
            return "";
        }
        return body.substring(q1 + 1, q2);
    }

    private String get(String url, String token) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(5000);
            conn.setRequestProperty("Authorization", "Bearer " + token);
            conn.setRequestProperty("Accept", "application/json");
            int code = conn.getResponseCode();
            if (code != 200) {
                return null;
            }
            InputStream in = conn.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            in.close();
            return out.toString("UTF-8");
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ------------------------------------------------------------ 通知

    private void createChannel() {
        createChannelStatic(this);
    }

    static void createChannelStatic(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(CH_ID,
                ctx.getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription(ctx.getString(R.string.notif_channel_desc));
        ch.enableVibration(true);
        nm.createNotificationChannel(ch);
    }

    private Notification foregroundNotification() {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CH_ID)
                .setContentTitle(getString(R.string.notif_service_title))
                .setContentText(getString(R.string.notif_service_text))
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void notifyOnce(int id, String title, String text, String tag) {
        notifyOnceStatic(this, id, title, text, tag);
    }

    static void notifyOnceStatic(Context ctx, int id, String title, String text, String tag) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        Intent open = new Intent(ctx, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        open.putExtra("tag", tag);
        PendingIntent pi = PendingIntent.getActivity(ctx, id, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(ctx, CH_ID)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        nm.notify(id, n);
    }

    /** 给网页调用的即时通知入口。 */
    public static void push(Context ctx, String title, String text) {
        if (ctx == null) {
            return;
        }
        createChannelStatic(ctx);
        notifyOnceStatic(ctx, NOTI_GENERAL, title, text, "page");
    }
}
