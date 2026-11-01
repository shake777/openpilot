package com.carrot.aaprobe

import android.app.Notification
import android.graphics.Bitmap
import android.graphics.drawable.Icon
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

/**
 * Records the guidance notifications TMAP and Naver Map post while navigating, on the same clock as
 * the M-CAN recording, so stock-HUD 0x4BB payloads can be matched against the phone's own turn
 * text/distance. Each distinct large-icon bitmap (usually the turn arrow) is saved once as PNG.
 */
class NavNotificationListener : NotificationListenerService() {
  companion object {
    val NAV_PACKAGES = mapOf(
      "com.skt.tmap.ku" to "tmap",
      "com.skt.skaf.l001mtm091" to "tmap",
      "com.nhn.android.nmap" to "naver",
    )
    private const val MAX_ICONS = 300
    @Volatile var connected = false
      private set
    @Volatile var lastSummary = ""
      private set
  }

  private val lastText = HashMap<String, String>()
  private val savedIcons = HashSet<String>()

  override fun onListenerConnected() {
    connected = true
    ProbeLog.init(applicationContext)
    ProbeLog.log("nav_notif_listener", mapOf("state" to "connected"))
    try {
      activeNotifications?.forEach { handle(it) }
    } catch (_: Exception) {}
  }

  override fun onListenerDisconnected() {
    connected = false
    ProbeLog.log("nav_notif_listener", mapOf("state" to "disconnected"))
  }

  override fun onNotificationPosted(sbn: StatusBarNotification) = handle(sbn)

  override fun onNotificationRemoved(sbn: StatusBarNotification) {
    val app = NAV_PACKAGES[sbn.packageName] ?: return
    lastText.remove("${sbn.packageName}:${sbn.id}")
    ProbeLog.log("nav_notif_removed", mapOf("app" to app, "id" to sbn.id))
  }

  private fun handle(sbn: StatusBarNotification) {
    val app = NAV_PACKAGES[sbn.packageName] ?: return
    val n = sbn.notification ?: return
    val ex = n.extras ?: Bundle()
    val fields = linkedMapOf<String, Any?>(
      "app" to app,
      "pkg" to sbn.packageName,
      "id" to sbn.id,
      "channel" to n.channelId,
      "title" to ex.getCharSequence(Notification.EXTRA_TITLE)?.toString(),
      "text" to ex.getCharSequence(Notification.EXTRA_TEXT)?.toString(),
      "sub" to ex.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString(),
      "big" to ex.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString(),
      "info" to ex.getCharSequence(Notification.EXTRA_INFO_TEXT)?.toString(),
      "lines" to ex.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)?.map { it.toString() },
      "custom_view" to (n.contentView != null || n.bigContentView != null),
    )
    val icon = iconKey(n)
    fields["icon"] = icon

    val key = "${sbn.packageName}:${sbn.id}"
    val textSig = fields.filterKeys { it != "id" }.toString()
    if (lastText.put(key, textSig) == textSig) return
    lastSummary = "$app ${fields["title"] ?: ""} ${fields["text"] ?: ""}".trim()
    // Keys only (not values) for anything the standard fields above do not cover.
    fields["extra_keys"] = ex.keySet()?.sorted()
    ProbeLog.log("nav_notif", fields)
  }

  /** Hash the large icon and store a PNG copy the first time each distinct image appears. */
  private fun iconKey(n: Notification): String? {
    val bmp: Bitmap = try {
      val icon: Icon? = n.getLargeIcon()
      val drawable = icon?.loadDrawable(this) ?: return null
      if (drawable is android.graphics.drawable.BitmapDrawable && drawable.bitmap != null) {
        drawable.bitmap
      } else {
        val w = drawable.intrinsicWidth.coerceIn(1, 256)
        val h = drawable.intrinsicHeight.coerceIn(1, 256)
        Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also {
          val c = android.graphics.Canvas(it)
          drawable.setBounds(0, 0, w, h)
          drawable.draw(c)
        }
      }
    } catch (_: Exception) {
      return null
    }
    val buf = java.nio.ByteBuffer.allocate(bmp.byteCount)
    bmp.copyPixelsToBuffer(buf)
    val hash = MessageDigest.getInstance("SHA-1").digest(buf.array())
      .take(6).joinToString("") { "%02x".format(it) }
    if (savedIcons.size < MAX_ICONS && savedIcons.add(hash)) {
      try {
        val dir = File(getExternalFilesDir(null) ?: filesDir, "nav_icons").apply { mkdirs() }
        val f = File(dir, "$hash.png")
        if (!f.exists()) FileOutputStream(f).use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
      } catch (_: Exception) {}
    }
    return hash
  }
}
