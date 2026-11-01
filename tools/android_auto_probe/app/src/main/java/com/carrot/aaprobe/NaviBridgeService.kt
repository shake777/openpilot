package com.carrot.aaprobe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder

/** Foreground service that keeps the TMAP/Naver Carrot Navi v2 bridge listening. */
class NaviBridgeService : Service() {
  companion object {
    const val PREF_UPSTREAM_MODE = "bridge_upstream_mode"  // "auto" | "manual" | "none"
    const val PREF_UPSTREAM_HOST = "bridge_upstream_host"
    const val PREF_UPSTREAM_PORT = "bridge_upstream_port"
    private const val CHANNEL = "bridge"
    private const val NOTIF_ID = 4012

    @Volatile var bridge: NaviBridge? = null
      private set
    @Volatile var lastStatus = "정지됨"
      private set

    fun start(context: Context) {
      val i = Intent(context, NaviBridgeService::class.java)
      if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, NaviBridgeService::class.java))
    }

    /** C4 receiver address: manual setting, or the Wi-Fi gateway (C4 hotspot) in auto mode. */
    fun upstream(context: Context): Pair<String?, Int> {
      val prefs = context.getSharedPreferences(ProbeLog.PREFS, Context.MODE_PRIVATE)
      val port = prefs.getInt(PREF_UPSTREAM_PORT, NaviBridge.PORT)
      return when (prefs.getString(PREF_UPSTREAM_MODE, "auto")) {
        "none" -> null to port
        "manual" -> {
          // Accepts "ip" or "ip:port".
          val raw = prefs.getString(PREF_UPSTREAM_HOST, "")?.trim().orEmpty()
          val host = raw.substringBefore(':').ifBlank { null }
          host to (raw.substringAfter(':', "").toIntOrNull() ?: port)
        }
        else -> wifiGateway(context) to port
      }
    }

    @Suppress("DEPRECATION")
    fun wifiGateway(context: Context): String? {
      val wifi = context.applicationContext.getSystemService(WifiManager::class.java) ?: return null
      val gw = wifi.dhcpInfo?.gateway ?: 0
      if (gw == 0) return null
      return "${gw and 0xFF}.${gw shr 8 and 0xFF}.${gw shr 16 and 0xFF}.${gw shr 24 and 0xFF}"
    }
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    ProbeLog.init(applicationContext)
    val nm = getSystemService(NotificationManager::class.java)
    nm.createNotificationChannel(NotificationChannel(CHANNEL, "내비 중계", NotificationManager.IMPORTANCE_LOW))
    val notif = notification("내비 중계 시작")
    if (Build.VERSION.SDK_INT >= 29) {
      startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    } else {
      startForeground(NOTIF_ID, notif)
    }
    if (bridge?.isRunning() != true) {
      val (host, port) = upstream(this)
      val b = NaviBridge(applicationContext) { msg ->
        lastStatus = msg
        nm.notify(NOTIF_ID, notification(msg))
      }
      try {
        b.start(NaviBridge.Config(host, port))
        bridge = b
      } catch (e: Exception) {
        lastStatus = "시작 실패: ${e.javaClass.simpleName} ${e.message ?: ""}"
        ProbeLog.log("bridge_status", mapOf("msg" to lastStatus))
        stopSelf()
      }
    }
    return START_STICKY
  }

  override fun onDestroy() {
    bridge?.stop()
    bridge = null
    lastStatus = "정지됨"
    super.onDestroy()
  }

  private fun notification(text: String): Notification {
    val open = PendingIntent.getActivity(
      this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
    )
    return Notification.Builder(this, CHANNEL)
      .setSmallIcon(R.drawable.ic_launcher)
      .setContentTitle("AA Probe 내비 중계")
      .setContentText(text)
      .setOngoing(true)
      .setContentIntent(open)
      .build()
  }
}
