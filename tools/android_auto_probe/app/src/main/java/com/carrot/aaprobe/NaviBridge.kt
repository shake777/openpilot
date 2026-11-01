package com.carrot.aaprobe

import android.content.Context
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.io.FilterInputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.SecureRandom
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Phone-local Carrot Navi v2 receiver for the patched TMAP (com.skt.tmap.ku) and Naver (CarrotNM).
 *
 * The bridge listens on 127.0.0.1:7714 and advertises that address on the phone's discovery port
 * 7705, so the sender connects here. With an upstream (the C4 receiver) it forwards every byte
 * unchanged in both directions and only reads the sender's guidance items on the way through, so
 * the C4 keeps getting exactly what it got before. Without an upstream it negotiates on its own and
 * enables only the guidance items. The latest guidance feeds the Android Auto relay in ProbeScreen.
 */
class NaviBridge(private val context: Context, private val onStatus: (String) -> Unit) {
  data class Config(val upstreamHost: String?, val upstreamPort: Int)

  data class Guide(
    val distanceM: Int,
    val timeSec: Int,
    val turnType: Int,
    val roadName: String,
    val mainText: String,
    val nearDirection: String,
  )

  data class State(
    val current: Guide? = null,
    val next: Guide? = null,
    val mode: String? = null,
    val remainM: Int? = null,
    val remainSec: Int? = null,
    val sender: String = "",
    val connections: Int = 0,
    val guideMono: Long = 0L,
  ) {
    fun guideAgeMs(): Long = if (guideMono == 0L) Long.MAX_VALUE else SystemClock.elapsedRealtime() - guideMono
  }

  companion object {
    const val PORT = 7714
    const val DISCOVERY_PORT = 7705
    private const val DISCOVERY_INTERVAL_MS = 250L
    /** After a failed C4 connection, how long to stop advertising so the sender reaches the C4 itself. */
    private const val UPSTREAM_YIELD_MS = 60_000L
    private const val PATH_PREFIX = "/api/navi/ws/v2/"
    /** The senders connect to the literal "127.0.0.1"; getLoopbackAddress() can be ::1. */
    private val LOOPBACK: InetAddress = InetAddress.getByAddress(byteArrayOf(127, 0, 0, 1))
    /** Items this bridge reads (and, standalone, the only ones it enables). */
    val GUIDANCE_ITEMS = setOf("guidance_current", "guidance_next", "navigation_status", "route", "vehicle")

    @Volatile var state = State()
      private set
    private val listeners = CopyOnWriteArrayList<(State) -> Unit>()
    fun addListener(l: (State) -> Unit) = listeners.add(l)
    fun removeListener(l: (State) -> Unit) = listeners.remove(l)
  }

  @Volatile private var running = false
  @Volatile private var yieldUntil = 0L
  private var server: ServerSocket? = null
  private val threads = CopyOnWriteArrayList<Thread>()
  private val sockets = CopyOnWriteArrayList<Socket>()
  private val rawLogged = HashMap<String, Int>()
  private var lastLoggedTurn = Int.MIN_VALUE
  private var lastLoggedDist = Int.MIN_VALUE
  private var lastLogMono = 0L

  fun isRunning() = running

  fun start(cfg: Config) {
    if (running) return
    running = true
    state = State()
    val srv = ServerSocket()
    srv.reuseAddress = true
    srv.bind(InetSocketAddress(LOOPBACK, PORT))
    server = srv
    val mode = cfg.upstreamHost?.let { "C4 중계 $it:${cfg.upstreamPort}" } ?: "단독"
    status("대기 중 127.0.0.1:$PORT ($mode)")
    ProbeLog.log("bridge_start", mapOf("upstream" to cfg.upstreamHost, "upstream_port" to cfg.upstreamPort))
    spawn("bridge-accept") {
      while (running) {
        val s = try { srv.accept() } catch (_: Exception) { break }
        sockets.add(s)
        spawn("bridge-conn") { handle(s, cfg) }
      }
    }
    spawn("bridge-discovery") { advertise() }
  }

  fun stop() {
    running = false
    try { server?.close() } catch (_: Exception) {}
    for (s in sockets) try { s.close() } catch (_: Exception) {}
    sockets.clear()
    for (t in threads) t.join(500)
    threads.clear()
    publish(State())
    status("정지됨")
  }

  private fun spawn(name: String, body: () -> Unit) {
    val t = Thread({
      try { body() } catch (_: Exception) {} finally { threads.remove(Thread.currentThread()) }
    }, name)
    threads.add(t)
    t.start()
  }

  private fun status(msg: String) {
    ProbeLog.log("bridge_status", mapOf("msg" to msg))
    onStatus(msg)
  }

  /** Point the sender at this bridge. Sent only to the phone itself; the C4 beacon is unchanged. */
  private fun advertise() {
    val body = JSONObject().put("ip", "127.0.0.1").put("navi_debug", 0).toString().toByteArray()
    DatagramSocket().use { sock ->
      val dst = LOOPBACK
      while (running) {
        if (SystemClock.elapsedRealtime() >= yieldUntil) {
          try {
            sock.send(DatagramPacket(body, body.size, dst, DISCOVERY_PORT))
          } catch (_: Exception) {}
        }
        Thread.sleep(DISCOVERY_INTERVAL_MS)
      }
    }
  }

  private fun handle(client: Socket, cfg: Config) {
    client.tcpNoDelay = true
    val cin = client.getInputStream()
    val cout = client.getOutputStream()
    val req = WebSocketIo.readRequest(cin)
    val rel = req.path.removePrefix(PATH_PREFIX)
    val kind = rel.substringBefore('/')
    val name = if (kind == "json") rel.substringAfterLast('/') else ""
    changeConnections(+1)
    try {
      if (cfg.upstreamHost != null) proxy(client, cin, cout, req, kind, name, cfg)
      else standalone(cin, cout, req, kind, name)
    } finally {
      changeConnections(-1)
      sockets.remove(client)
      try { client.close() } catch (_: Exception) {}
    }
  }

  // ---- proxy: byte-exact pass-through to the C4, reading guidance on the way ----

  private fun proxy(client: Socket, cin: InputStream, cout: OutputStream, req: WebSocketIo.Request,
                    kind: String, name: String, cfg: Config) {
    val up = Socket()
    try {
      up.tcpNoDelay = true
      up.connect(InetSocketAddress(cfg.upstreamHost, cfg.upstreamPort), 3000)
    } catch (e: Exception) {
      // Never leave the C4 without guidance: stop advertising so the sender's next discovery pass
      // picks up the C4's own beacon and connects to it directly.
      yieldUntil = SystemClock.elapsedRealtime() + UPSTREAM_YIELD_MS
      status("C4 연결 실패 ${cfg.upstreamHost}:${cfg.upstreamPort} (${e.javaClass.simpleName}) → ${UPSTREAM_YIELD_MS / 1000}초간 C4 직접 연결로 양보")
      try { up.close() } catch (_: Exception) {}
      return
    }
    sockets.add(up)
    val uin = up.getInputStream()
    val uout = up.getOutputStream()
    uout.write(req.head)
    uout.flush()
    if (kind == "control") status("TMAP/네이버 연결됨 → C4 중계 중")
    spawn("bridge-down") {
      val buf = ByteArray(16 * 1024)
      try {
        while (true) {
          val n = uin.read(buf)
          if (n < 0) break
          cout.write(buf, 0, n)
          cout.flush()
        }
      } catch (_: Exception) {
      } finally {
        try { client.shutdownOutput() } catch (_: Exception) {}
      }
    }
    try {
      if (kind == "control" || name in GUIDANCE_ITEMS) {
        val tee = TeeInputStream(cin, uout)
        while (true) {
          val m = WebSocketIo.readMessage(tee)
          if (m.opcode == WebSocketIo.OP_CLOSE) break
          if (m.opcode == WebSocketIo.OP_TEXT) onText(kind, name, m.text())
        }
        // Drain whatever follows the close frame.
        val buf = ByteArray(4096)
        while (tee.read(buf) >= 0) {}
      } else {
        val buf = ByteArray(64 * 1024)
        while (true) {
          val n = cin.read(buf)
          if (n < 0) break
          uout.write(buf, 0, n)
          uout.flush()
        }
      }
    } catch (_: Exception) {
    } finally {
      try { up.close() } catch (_: Exception) {}
      sockets.remove(up)
    }
  }

  /** Copies every byte read from the sender to the upstream, preserving the stream exactly. */
  private class TeeInputStream(input: InputStream, private val copy: OutputStream) : FilterInputStream(input) {
    override fun read(): Int {
      val b = super.read()
      if (b >= 0) { copy.write(b); copy.flush() }
      return b
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
      val n = super.read(b, off, len)
      if (n > 0) { copy.write(b, off, n); copy.flush() }
      return n
    }
  }

  // ---- standalone: negotiate ourselves, enable guidance items only ----

  private fun standalone(cin: InputStream, cout: OutputStream, req: WebSocketIo.Request, kind: String, name: String) {
    cout.write(WebSocketIo.acceptResponse(req))
    cout.flush()
    if (kind == "control") status("TMAP/네이버 연결됨 (단독)")
    while (running) {
      val m = WebSocketIo.readMessage(cin)
      when (m.opcode) {
        WebSocketIo.OP_PING -> WebSocketIo.writeFrame(cout, WebSocketIo.OP_PONG, m.payload)
        WebSocketIo.OP_CLOSE -> {
          WebSocketIo.writeFrame(cout, WebSocketIo.OP_CLOSE, m.payload.take(2).toByteArray())
          return
        }
        WebSocketIo.OP_TEXT -> {
          val text = m.text()
          if (kind == "control") {
            val reply = controlReply(text)
            if (reply != null) WebSocketIo.writeFrame(cout, WebSocketIo.OP_TEXT, reply.toByteArray())
          }
          onText(kind, name, text)
        }
      }
    }
  }

  private fun controlReply(text: String): String? {
    val msg = try { JSONObject(text) } catch (_: Exception) { return null }
    if (msg.optString("type") != "requirements_query") return null
    val offered = msg.optJSONArray("streams") ?: return null
    val streams = JSONArray()
    for (i in 0 until offered.length()) {
      val o = offered.optJSONObject(i) ?: continue
      val kind = o.optString("kind")
      val name = o.optString("name")
      streams.put(JSONObject()
        .put("kind", kind)
        .put("name", name)
        .put("schema_version", o.optInt("schema_version", 1))
        .put("stream_handle", i + 1)
        .put("enabled", kind == "json" && name in GUIDANCE_ITEMS)
        .put("params", defaultParams(kind, name)))
    }
    val session = ByteArray(8).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
    return JSONObject()
      .put("type", "subscription_manifest")
      .put("protocol_version", 2)
      .put("session_id", session)
      .put("revision", 1)
      .put("metrics_enabled", false)
      .put("limit_adjustments", JSONArray())
      .put("streams", streams)
      .toString()
  }

  /** Same defaults as the C4 receiver (selfdrive/carrot/carrot_navi.py _stream_params). */
  private fun defaultParams(kind: String, name: String): JSONObject = when (kind) {
    "json" -> JSONObject().put("delivery_mode", "on_change").put("interval_ms", 1000).put("stale_timeout_ms", 10000)
    "image" -> JSONObject().put("format", "png").put("max_fps", 5).put("stale_timeout_ms", 15000).also {
      if (name.startsWith("crossroad_")) it.put("theme", "auto")
    }
    else -> JSONObject()
      .put("composition", "map_route_vehicle").put("width", 960).put("height", 540).put("dpi", 360)
      .put("fps", 10).put("jpeg_quality", 75).put("codec", "h264").put("h264_bitrate_kbps", 3000)
      .put("h264_keyframe_interval_sec", 2).put("camera_mode", "app_sync").put("map_theme", "auto")
      .put("map_type", "normal").put("zoom", 11.0).put("tilt", 50.0).put("bearing", 0.0)
      .put("follow_vehicle_bearing", true).put("fov", 40.0).put("screen_center_y_ratio", 0.8)
      .put("follow_vehicle", true).put("center_latitude", JSONObject.NULL)
      .put("center_longitude", JSONObject.NULL).put("stale_timeout_ms", 5000)
  }

  // ---- guidance parsing ----

  private fun onText(kind: String, name: String, text: String) {
    val msg = try { JSONObject(text) } catch (_: Exception) { return }
    if (kind == "control") {
      when (msg.optString("type")) {
        "requirements_query" -> {
          val v = msg.optString("app_version")
          publish(state.copy(sender = v))
          ProbeLog.log("bridge_sender", mapOf("app_version" to v))
        }
        "manifest_applied", "manifest_rejected" ->
          ProbeLog.log("bridge_manifest", mapOf("type" to msg.optString("type"), "error" to msg.optString("error")))
      }
      return
    }
    if (msg.optString("type") != "item_update") return
    val n = rawLogged.getOrDefault(name, 0)
    if (n < 3) {
      rawLogged[name] = n + 1
      ProbeLog.log("bridge_raw", mapOf("name" to name, "msg" to msg))
    }
    val present = msg.optBoolean("present", false)
    val v = msg.optJSONObject("value")
    val next = synchronized(this) {
      val s = state
      val updated = when (name) {
      "guidance_current" -> s.copy(current = if (present && v != null) guide(v) else null, guideMono = SystemClock.elapsedRealtime())
      "guidance_next" -> s.copy(next = if (present && v != null) guide(v) else null)
      "navigation_status" -> s.copy(mode = if (present && v != null) v.optString("mode", "") else null)
      "route" -> if (present && v != null) {
        s.copy(remainM = v.optIntOrNull("remain_distance_m"), remainSec = v.optIntOrNull("remain_time_sec"))
      } else s.copy(remainM = null, remainSec = null)
        else -> return
      }
      publish(updated)
      updated
    }
    if (name == "guidance_current") logGuide(next.current)
  }

  private fun logGuide(g: Guide?) {
    val now = SystemClock.elapsedRealtime()
    val turn = g?.turnType ?: -1
    val dist = g?.distanceM ?: -1
    val big = turn != lastLoggedTurn || (dist / 50 != lastLoggedDist / 50 && now - lastLogMono >= 500)
    if (!big) return
    lastLoggedTurn = turn
    lastLoggedDist = dist
    lastLogMono = now
    ProbeLog.log("bridge_guide", mapOf(
      "present" to (g != null), "turn_type" to turn, "distance_m" to dist,
      "maneuver" to g?.let { TurnMap.of(it.turnType).label }, "road" to g?.roadName, "text" to g?.mainText,
    ))
  }

  private fun guide(v: JSONObject) = Guide(
    distanceM = v.optInt("distance_m", 0),
    timeSec = v.optInt("time_sec", 0),
    turnType = v.optInt("turn_type", -1),
    roadName = v.optString("road_name", "").takeIf { it != "null" } ?: "",
    mainText = v.optString("main_text", "").takeIf { it != "null" } ?: "",
    nearDirection = v.optString("near_direction", "").takeIf { it != "null" } ?: "",
  )

  private fun JSONObject.optIntOrNull(key: String): Int? = if (isNull(key) || !has(key)) null else optInt(key)

  @Synchronized
  private fun changeConnections(delta: Int) {
    publish(state.copy(connections = (state.connections + delta).coerceAtLeast(0)))
  }

  @Synchronized
  private fun publish(s: State) {
    state = s
    for (l in listeners) try { l(s) } catch (_: Exception) {}
  }
}
