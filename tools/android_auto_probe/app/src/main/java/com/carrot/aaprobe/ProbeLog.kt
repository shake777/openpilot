package com.carrot.aaprobe

import android.content.Context
import android.location.Location
import android.os.SystemClock
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * JSON-lines probe log: logcat (tag AAProbe), a per-run file in the app's external files dir,
 * and an optional UDP copy so the same lines can be watched live on a PC or the C4.
 */
object ProbeLog {
  private const val TAG = "AAProbe"
  const val PREFS = "aaprobe"
  const val PREF_UDP_ENABLED = "udp_enabled"
  const val PREF_UDP_HOST = "udp_host"
  const val PREF_UDP_PORT = "udp_port"
  const val DEFAULT_UDP_PORT = 7799
  private const val RECENT_MAX = 300

  private val io = Executors.newSingleThreadExecutor()
  private val recent = ArrayDeque<String>()
  private var writer: BufferedWriter? = null
  private var socket: DatagramSocket? = null

  @Volatile private var udpEnabled = false
  @Volatile private var udpHost = ""
  @Volatile private var udpPort = DEFAULT_UDP_PORT

  @Volatile var logFile: File? = null
    private set

  @Synchronized
  fun init(context: Context) {
    reloadUdp(context)
    if (writer != null) return
    val dir = context.getExternalFilesDir(null) ?: context.filesDir
    val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
    val file = File(dir, "aaprobe-$stamp.jsonl")
    logFile = file
    writer = BufferedWriter(FileWriter(file, true))
    log("log_open", mapOf("file" to file.absolutePath))
  }

  fun reloadUdp(context: Context) {
    val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    udpEnabled = prefs.getBoolean(PREF_UDP_ENABLED, false)
    udpHost = prefs.getString(PREF_UDP_HOST, "") ?: ""
    udpPort = prefs.getInt(PREF_UDP_PORT, DEFAULT_UDP_PORT)
  }

  fun log(kind: String, fields: Map<String, Any?> = emptyMap()) {
    val obj = JSONObject()
    obj.put("t", System.currentTimeMillis())
    obj.put("mono_ms", SystemClock.elapsedRealtime())
    obj.put("kind", kind)
    for ((k, v) in fields) obj.put(k, toJson(v))
    val line = obj.toString()
    Log.i(TAG, line)
    synchronized(recent) {
      recent.addLast(line)
      while (recent.size > RECENT_MAX) recent.removeFirst()
    }
    io.execute {
      try {
        writer?.apply {
          write(line)
          newLine()
          flush()
        }
      } catch (e: Exception) {
        Log.w(TAG, "file write failed", e)
      }
      sendUdp(line)
    }
  }

  fun recentLines(n: Int): List<String> = synchronized(recent) { recent.toList().takeLast(n) }

  private fun sendUdp(line: String) {
    val host = udpHost
    if (!udpEnabled || host.isBlank()) return
    try {
      val sock = socket ?: DatagramSocket().also {
        it.broadcast = true
        socket = it
      }
      val bytes = line.toByteArray(Charsets.UTF_8)
      sock.send(DatagramPacket(bytes, bytes.size, InetAddress.getByName(host), udpPort))
    } catch (e: Exception) {
      Log.w(TAG, "udp send failed", e)
    }
  }

  fun toJson(v: Any?): Any = when (v) {
    null -> JSONObject.NULL
    is JSONObject, is JSONArray, is String, is Boolean, is Int, is Long -> v
    is Float -> if (v.isFinite()) v.toDouble() else v.toString()
    is Double -> if (v.isFinite()) v else v.toString()
    is Number -> v
    is Location -> JSONObject().apply {
      put("lat", v.latitude)
      put("lon", v.longitude)
      put("acc_m", if (v.hasAccuracy()) v.accuracy.toDouble() else JSONObject.NULL)
      put("speed_mps", if (v.hasSpeed()) v.speed.toDouble() else JSONObject.NULL)
      put("bearing_deg", if (v.hasBearing()) v.bearing.toDouble() else JSONObject.NULL)
      put("alt_m", if (v.hasAltitude()) v.altitude else JSONObject.NULL)
      put("fix_time", v.time)
      put("provider", v.provider ?: "")
    }
    is Map<*, *> -> JSONObject().apply { for ((k, value) in v) put(k.toString(), toJson(value)) }
    is Iterable<*> -> JSONArray().apply { for (item in v) put(toJson(item)) }
    is Array<*> -> JSONArray().apply { for (item in v) put(toJson(item)) }
    else -> v.toString()
  }
}
