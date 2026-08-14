package com.example.m200funlocker;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/** Native Android USB-host client for the M200F's on-device fingerprint flow. */
public final class MainActivity extends android.app.Activity {
    private static final int VID = 0x21C4, PID = 0x8381;
    private static final String ACTION_USB_PERMISSION = "com.example.m200funlocker.USB_PERMISSION";
    private static final int BG = 0xff121212, CARD = 0xff1e1e1e, INK = 0xffe0e0e0;
    private static final int IDLE = 0xff5c5c5c, BLUE = 0xff00a8ff, GREEN = 0xff00e676;
    private static final int YELLOW = 0xffffca28, RED = 0xffff5252;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final AtomicBoolean destroyed = new AtomicBoolean();
    private final AtomicInteger session = new AtomicInteger();
    private volatile BotTransport activeTransport;
    private UsbManager usbManager;
    private TextView status, deviceLabel, attempts, log;
    private RadarView radar;
    private Button retry;

    private final BroadcastReceiver permissionReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (!ACTION_USB_PERMISSION.equals(intent.getAction())) return;
            UsbDevice device = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
            int id = session.get();
            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false) && device != null) start(device, id);
            else if (isActive(id)) showStatus("未获得 USB 访问权限", RED);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        usbManager = (UsbManager) getSystemService(USB_SERVICE);
        registerReceiver(permissionReceiver, new IntentFilter(ACTION_USB_PERMISSION), RECEIVER_NOT_EXPORTED);
        setContentView(buildUi());
        retry.setOnClickListener(v -> findAndRequest());
        findAndRequest();
    }

    @Override public void onDestroy() {
        destroyed.set(true);
        session.incrementAndGet();
        closeActiveTransport();
        worker.shutdownNow();
        unregisterReceiver(permissionReceiver);
        super.onDestroy();
    }

    private View buildUi() {
        LinearLayout page = column();
        page.setBackgroundColor(BG);
        page.setPadding(dp(20), dp(20), dp(20), dp(20));

        LinearLayout header = card();
        header.addView(label("M200F 指纹解锁", 20, INK));
        header.addView(label("HIKVISION 安全 U 盘 · 硬件指纹验证", 13, 0xff9e9e9e));
        page.addView(header, match());

        LinearLayout stateCard = card();
        stateCard.setGravity(Gravity.CENTER_HORIZONTAL);
        stateCard.setPadding(dp(16), dp(24), dp(16), dp(20));
        radar = new RadarView(this);
        stateCard.addView(radar, new LinearLayout.LayoutParams(-1, dp(190)));
        status = label("正在检测设备...", 22, INK);
        status.setGravity(Gravity.CENTER);
        stateCard.addView(status, match());
        LinearLayout chips = new LinearLayout(this);
        chips.setGravity(Gravity.CENTER);
        chips.setPadding(0, dp(14), 0, 0);
        deviceLabel = chip("设备: --");
        attempts = chip("尝试: 0");
        chips.addView(deviceLabel);
        chips.addView(attempts);
        stateCard.addView(chips, match());
        page.addView(stateCard, match());

        LinearLayout logCard = card();
        logCard.setPadding(dp(16), dp(14), dp(16), dp(14));
        logCard.addView(label("运行日志", 12, 0xff9e9e9e));
        log = label("", 13, 0xffa0a0a0);
        log.setTypeface(android.graphics.Typeface.MONOSPACE);
        log.setPadding(0, dp(8), 0, 0);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(log);
        logCard.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        page.addView(logCard, new LinearLayout.LayoutParams(-1, 0, 1));

        retry = new Button(this);
        retry.setText("重新检测");
        retry.setTextColor(INK);
        retry.setBackgroundColor(0xff003c5a);
        LinearLayout.LayoutParams retryParams = match();
        retryParams.gravity = Gravity.END;
        retryParams.topMargin = dp(14);
        page.addView(retry, retryParams);
        return page;
    }

    private void findAndRequest() {
        int id = session.incrementAndGet();
        closeActiveTransport();
        ui.post(() -> attempts.setText("尝试: 0"));
        showStatus("正在检测设备...", IDLE);
        append("正在检测 M200F 设备 (VID 21C4 / PID 8381)...");
        UsbDevice found = null;
        for (Map.Entry<String, UsbDevice> entry : usbManager.getDeviceList().entrySet()) {
            UsbDevice device = entry.getValue();
            if (device.getVendorId() == VID && device.getProductId() == PID) {
                found = device;
                break;
            }
        }
        if (found == null) {
            showStatus("未检测到 M200F", RED);
            append("请通过 OTG 连接 U 盘后重新检测。");
            return;
        }
        deviceLabel.setText(String.format(Locale.US, "USB %04X:%04X", VID, PID));
        if (usbManager.hasPermission(found)) start(found, id);
        else {
            PendingIntent pending = PendingIntent.getBroadcast(this, 0,
                    new Intent(ACTION_USB_PERMISSION), PendingIntent.FLAG_IMMUTABLE);
            usbManager.requestPermission(found, pending);
        }
    }

    private void start(UsbDevice device, int id) {
        worker.submit(() -> runProtocol(device, id));
    }

    private void runProtocol(UsbDevice device, int id) {
        BotTransport transport = null;
        try {
            transport = BotTransport.open(usbManager, device);
            activeTransport = transport;
            if (!isActive(id)) return;
            byte[] firmware = transport.scsi(hex("A1 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00"), 128, 0);
            if (!new String(firmware, StandardCharsets.US_ASCII).contains("DM8381")) {
                throw new Exception("设备未返回 DM8381 固件标识");
            }
            append("检测到 M200F，开始完整初始化...");
            runInitialization(transport, id);
            if (!resetAndWait(transport, id)) return;
            append("初始化完成，请将手指放到传感器上。");
            showStatus("请放上指纹", BLUE);

            int attempt = 0;
            boolean fingerDown = false;
            while (isActive(id)) {
                byte[] response = poll(transport, 0x01);
                if (same(response, 0x01, 0x02, 0xff)) {
                    fingerDown = false;
                    showStatus("请放上指纹", BLUE);
                    sleep(350);
                    continue;
                }
                if (isVerified(response)) {
                    verified(response);
                    return;
                }
                if (same(response, 0xff, 0xfd, 0xff)) {
                    append("识别失败，正在复位传感器...");
                    showStatus("识别失败", RED);
                    if (!resetAndWait(transport, id)) return;
                    fingerDown = false;
                    showStatus("请放上指纹", BLUE);
                    continue;
                }
                if (isScanState(response)) {
                    if (fingerDown) {
                        sleep(350);
                        continue;
                    }
                    fingerDown = true;
                    attempt++;
                    final int displayAttempt = attempt;
                    ui.post(() -> attempts.setText("尝试: " + displayAttempt));
                    append("检测到手指，正在识别...");
                    showStatus("检测中", YELLOW);
                    if (scanFingerprint(transport, id)) return;
                    if (!resetAndWait(transport, id)) return;
                    fingerDown = false;
                    showStatus("请放上指纹", BLUE);
                    continue;
                }
                append("未知传感器响应: " + toHex(response));
                sleep(300);
            }
        } catch (Exception error) {
            if (isActive(id)) {
                append("USB 通信错误: " + error.getMessage());
                showStatus("通信中断，正在重新检测", RED);
                retryLater(id);
            }
        } finally {
            if (transport != null) transport.close();
            if (activeTransport == transport) activeTransport = null;
        }
    }

    private void runInitialization(BotTransport transport, int id) throws Exception {
        int[][] init = {{0,8192},{0,128},{0,1053},{0,1},{2,1},{1,1},{0,512},{0,1},{0,35},{0,1}};
        String[] cdb = {
                "28 00 00 00 00 00 00 00 10 00 00 00 00 00 00 00",
                "A1 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00",
                "A1 01 00 04 1D 00 00 00 00 00 00 00 00 00 00 00",
                "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00",
                "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00",
                "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00",
                "A1 00 00 00 02 00 00 00 00 00 00 00 00 00 00 00",
                "90 29 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
                "90 2B 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
                "90 26 00 00 00 00 00 00 00 00 00 00 00 00 00 00"};
        for (int i = 0; i < cdb.length && isActive(id); i++) {
            byte[] data = transport.scsi(hex(cdb[i]), init[i][1], init[i][0]);
            append("  初始化 LUN" + init[i][0] + ": " + data.length + " B");
        }
    }

    private boolean scanFingerprint(BotTransport transport, int id) throws Exception {
        poll(transport, 0x02);
        for (int cycle = 0; cycle < 6 && isActive(id); cycle++) {
            poll(transport, 0x03);
            long deadline = System.currentTimeMillis() + 3000;
            byte[] response;
            do {
                response = poll(transport, 0x04);
                if (same(response, 0x04, 0x05, 0xff)) sleep(40);
            } while (same(response, 0x04, 0x05, 0xff) && System.currentTimeMillis() < deadline && isActive(id));

            response = poll(transport, 0x05);
            if (isVerified(response)) {
                verified(response);
                return true;
            }
            if (same(response, 0xff, 0xfd, 0xff)) {
                append("本次指纹未匹配。");
                return false;
            }
            if (same(response, 0x03, 0x04, 0xff)) continue;
            append("本轮扫描结束，响应: " + toHex(response));
            return false;
        }
        append("识别未成功，请抬起手指后重试。");
        return false;
    }

    /** Same recovery loop as the Python GUI: reset until the sensor is idle. */
    private boolean resetAndWait(BotTransport transport, int id) throws Exception {
        boolean liftHintLogged = false;
        while (isActive(id)) {
            for (int i = 0; i < 3 && isActive(id); i++) {
                poll(transport, 0x00);
                sleep(50);
            }
            byte[] response = poll(transport, 0x01);
            if (same(response, 0x01, 0x02, 0xff) || isVerified(response)) return true;
            if (isScanState(response)) {
                if (!liftHintLogged) {
                    append("手指仍按在传感器上，请抬起后继续。");
                    liftHintLogged = true;
                }
                sleep(350);
            } else {
                append("设备尚未回到空闲状态: " + toHex(response));
                sleep(500);
            }
        }
        return false;
    }

    private void retryLater(int failedId) {
        ui.postDelayed(() -> {
            if (isActive(failedId)) findAndRequest();
        }, 1000);
    }

    private byte[] poll(BotTransport transport, int parameter) throws Exception {
        byte[] cdb = new byte[16];
        cdb[0] = (byte) 0x90;
        cdb[1] = 0x2f;
        cdb[6] = (byte) parameter;
        return transport.scsi(cdb, 3, 0);
    }

    private void verified(byte[] response) {
        int fingerprintId = response.length > 2 ? response[2] & 0xff : 0;
        append("指纹 " + fingerprintId + " 验证成功，设备已解锁加密分区。");
        // Keep the claimed interface briefly so Android vold sees a settled LUN1
        // when it starts mounting the newly unlocked volume.
        showStatus("指纹验证成功，等待挂载分区...", GREEN);
        sleep(2000);
    }

    private boolean isActive(int id) { return !destroyed.get() && session.get() == id; }
    private void closeActiveTransport() { BotTransport old = activeTransport; if (old != null) old.close(); activeTransport = null; }
    private static boolean isVerified(byte[] value) { return value.length > 0 && (value[0] & 0xff) == 0x0e; }
    private static boolean isScanState(byte[] value) { return same(value, 2, 3, 255) || same(value, 3, 4, 255) || same(value, 4, 5, 255) || same(value, 5, 6, 255); }
    private static boolean same(byte[] value, int a, int b, int c) { return value.length >= 3 && (value[0]&255)==a && (value[1]&255)==b && (value[2]&255)==c; }
    private static String toHex(byte[] value) { StringBuilder out = new StringBuilder(); for (byte b : value) out.append(String.format(Locale.US, "%02X ", b & 255)); return out.toString().trim(); }
    private static byte[] hex(String text) { String[] parts = text.split(" "); byte[] output = new byte[parts.length]; for (int i=0;i<parts.length;i++) output[i]=(byte)Integer.parseInt(parts[i],16); return output; }
    private void showStatus(String text, int color) { ui.post(() -> { status.setText(text); status.setTextColor(color); radar.setColor(color); }); }
    private void append(String text) { ui.post(() -> { log.append(text + "\n"); }); }
    private static void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } }
    private LinearLayout column() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private LinearLayout card() { LinearLayout view = column(); view.setBackgroundColor(CARD); view.setPadding(dp(16),dp(16),dp(16),dp(16)); LinearLayout.LayoutParams params=match(); params.bottomMargin=dp(16); view.setLayoutParams(params); return view; }
    private TextView label(String text, int size, int color) { TextView view=new TextView(this); view.setText(text); view.setTextSize(size); view.setTextColor(color); return view; }
    private TextView chip(String text) { TextView view=label(text,12,INK); view.setBackgroundColor(0xff2c2c2c); view.setPadding(dp(10),dp(6),dp(10),dp(6)); LinearLayout.LayoutParams params=new LinearLayout.LayoutParams(-2,-2); params.setMargins(dp(4),0,dp(4),0); view.setLayoutParams(params); return view; }
    private LinearLayout.LayoutParams match() { return new LinearLayout.LayoutParams(-1,-2); }
    private int dp(int value) { return (int)(value * getResources().getDisplayMetrics().density + .5f); }

    /** Canvas animation always measures from the shorter side, so circles never become ellipses. */
    private static final class RadarView extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private float phase;
        private int color = IDLE;
        RadarView(Context context) { super(context); post(tick); }
        private final Runnable tick = new Runnable() { @Override public void run() { phase += 0.12f; invalidate(); postDelayed(this, 35); } };
        void setColor(int value) { color = value; invalidate(); }
        @Override protected void onDetachedFromWindow() { removeCallbacks(tick); super.onDetachedFromWindow(); }
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float cx = getWidth() / 2f, cy = getHeight() / 2f;
            float base = Math.min(getWidth(), getHeight()) * 0.29f;
            float glow = (float) ((Math.sin(phase) + 1d) / 2d);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(getResources().getDisplayMetrics().density * 2f);
            for (int i = 4; i > 0; i--) {
                paint.setColor(withAlpha(color, (int) (glow * (5 - i) * 35)));
                canvas.drawCircle(cx, cy, base + i * base * 0.13f * glow, paint);
            }
            paint.setColor(color);
            canvas.drawCircle(cx, cy, base, paint);
            paint.setStyle(Paint.Style.FILL);
            canvas.drawCircle(cx, cy, base * (0.16f + glow * 0.08f), paint);
        }
        private static int withAlpha(int color, int alpha) { return (color & 0x00ffffff) | (Math.min(255, alpha) << 24); }
    }

    /** USB Mass Storage Bulk-Only Transport: CBW -> data IN -> CSW. */
    private static final class BotTransport implements AutoCloseable {
        private final UsbDeviceConnection conn;
        private final UsbInterface iface;
        private final UsbEndpoint out, in;
        private boolean closed;
        private int tag;
        private BotTransport(UsbDeviceConnection c, UsbInterface i, UsbEndpoint o, UsbEndpoint n) { conn=c; iface=i; out=o; in=n; }
        static BotTransport open(UsbManager manager, UsbDevice device) throws Exception {
            UsbInterface selected = null; UsbEndpoint out = null, in = null;
            for (int i = 0; i < device.getInterfaceCount(); i++) {
                UsbInterface candidate = device.getInterface(i);
                if (candidate.getInterfaceClass() != UsbConstants.USB_CLASS_MASS_STORAGE) continue;
                UsbEndpoint candidateOut = null, candidateIn = null;
                for (int endpoint = 0; endpoint < candidate.getEndpointCount(); endpoint++) {
                    UsbEndpoint value = candidate.getEndpoint(endpoint);
                    if (value.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK) continue;
                    if (value.getDirection() == UsbConstants.USB_DIR_IN) candidateIn = value; else candidateOut = value;
                }
                if (candidateIn != null && candidateOut != null) { selected = candidate; in = candidateIn; out = candidateOut; break; }
            }
            if (selected == null) throw new Exception("未找到 Bulk Mass Storage 接口");
            UsbDeviceConnection connection = manager.openDevice(device);
            if (connection == null || !connection.claimInterface(selected, true)) throw new Exception("Android 无法声明 USB Mass Storage 接口");
            return new BotTransport(connection, selected, out, in);
        }
        synchronized byte[] scsi(byte[] cdb, int length, int lun) throws Exception {
            if (closed) throw new Exception("USB 会话已关闭");
            int currentTag = tag++;
            byte[] cbw = new byte[31];
            put32(cbw, 0, 0x43425355); put32(cbw, 4, currentTag); put32(cbw, 8, length);
            cbw[12] = (byte) 0x80; cbw[13] = (byte) lun; cbw[14] = 16;
            System.arraycopy(cdb, 0, cbw, 15, Math.min(cdb.length, 16));
            write(cbw);
            byte[] data = readExactly(length);
            byte[] csw = readExactly(13);
            if (get32(csw, 0) != 0x53425355 || get32(csw, 4) != currentTag || csw[12] != 0) {
                bulkOnlyReset();
                throw new Exception("SCSI 命令失败 (CSW=" + toHex(csw) + ")");
            }
            return data;
        }
        private void write(byte[] data) throws Exception { if (conn.bulkTransfer(out, data, data.length, 10000) != data.length) throw new Exception("CBW 发送失败"); }
        private byte[] readExactly(int size) throws Exception {
            byte[] all = new byte[size]; int offset = 0;
            while (offset < size) {
                byte[] part = new byte[Math.min(16384, size - offset)];
                int count = conn.bulkTransfer(in, part, part.length, 10000);
                if (count <= 0) throw new Exception("USB 读取超时");
                System.arraycopy(part, 0, all, offset, count); offset += count;
            }
            return all;
        }
        private void bulkOnlyReset() { conn.controlTransfer(0x21, 0xff, 0, iface.getId(), null, 0, 2000); }
        private static void put32(byte[] bytes, int offset, int value) { bytes[offset]=(byte)value; bytes[offset+1]=(byte)(value>>>8); bytes[offset+2]=(byte)(value>>>16); bytes[offset+3]=(byte)(value>>>24); }
        private static int get32(byte[] bytes, int offset) { return (bytes[offset]&255) | ((bytes[offset+1]&255)<<8) | ((bytes[offset+2]&255)<<16) | ((bytes[offset+3]&255)<<24); }
        @Override public synchronized void close() { if (!closed) { closed = true; conn.releaseInterface(iface); conn.close(); } }
    }
}
