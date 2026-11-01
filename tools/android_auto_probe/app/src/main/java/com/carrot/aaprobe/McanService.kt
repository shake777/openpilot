package com.carrot.aaprobe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/** Foreground service that keeps the receive-only M-CAN recorder alive while the screen is mirrored. */
class McanService : Service() {
  companion object {
    const val PREF_HOST = "mcan_host"
    const val PREF_PORT = "mcan_port"
    const val PREF_BITRATE = "mcan_bitrate"
    const val PREF_SILENT = "mcan_adapter_silent"
    const val DEFAULT_HOST = "192.168.80.1"
    const val DEFAULT_PORT = 3333
    private const val CHANNEL = "mcan"
    private const val NOTIF_ID = 4011

    @Volatile var link: McanLink? = null
      private set
    @Volatile var lastStatus = "정지됨"
      private set

    fun start(context: Context) {
      val i = Intent(context, McanService::class.java)
      if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, McanService::class.java))
    }
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    ProbeLog.init(applicationContext)
    val nm = getSystemService(NotificationManager::class.java)
    nm.createNotificationChannel(NotificationChannel(CHANNEL, "M-CAN 기록", NotificationManager.IMPORTANCE_LOW))
    val notif = notification("M-CAN 기록 시작")
    if (Build.VERSION.SDK_INT >= 29) {
      startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    } else {
      startForeground(NOTIF_ID, notif)
    }

    if (link?.isRunning() != true) {
      val prefs = getSharedPreferences(ProbeLog.PREFS, Context.MODE_PRIVATE)
      val cfg = McanLink.Config(
        host = prefs.getString(PREF_HOST, DEFAULT_HOST) ?: DEFAULT_HOST,
        port = prefs.getInt(PREF_PORT, DEFAULT_PORT),
        bitrate = prefs.getInt(PREF_BITRATE, 0),
        adapterSilent = prefs.getBoolean(PREF_SILENT, false),
      )
      val l = McanLink(applicationContext) { msg ->
        lastStatus = msg
        nm.notify(NOTIF_ID, notification(msg))
      }
      link = l
      l.start(cfg)
    }
    return START_STICKY
  }

  override fun onDestroy() {
    link?.stop()
    link = null
    lastStatus = "정지됨"
    super.onDestroy()
  }

  private fun notification(text: String): Notification {
    val open = PendingIntent.getActivity(
      this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
    )
    return Notification.Builder(this, CHANNEL)
      .setSmallIcon(R.drawable.ic_launcher)
      .setContentTitle("AA Probe M-CAN (수신 전용)")
      .setContentText(text)
      .setOngoing(true)
      .setContentIntent(open)
      .build()
  }
}
