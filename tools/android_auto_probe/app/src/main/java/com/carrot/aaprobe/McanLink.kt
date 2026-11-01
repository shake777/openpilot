package com.carrot.aaprobe

import android.content.Context
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

/**
 * Receive-only M-CAN recorder over an SLCAN-over-TCP adapter (e.g. WiCAN in slcan mode).
 *
 * Opens the channel with SLCAN "L" (listen-only) and never sends a data frame: no "t"/"T" command
 * exists in this file. If the adapter rejects "L" the session stops instead of opening normally,
 * unless the user states that the adapter itself is configured silent.
 *
 * Every received frame goes to a candump-style file (mcan-*.log); the probe log gets per-ID
 * summaries and each payload change of the HU->cluster TBT/projection channels.
 */
class McanLink(private val context: Context, private val onStatus: (String) -> Unit) {
  data class Config(
    val host: String,
    val port: Int,
    val bitrate: Int,          // bit/s, or 0 = scan
    val adapterSilent: Boolean, // adapter is configured silent; open with "O" if "L" is rejected
  )

  companion object {
    /** SLCAN Sx codes. */
    val BITRATES = linkedMapOf(
      10_000 to 0, 20_000 to 1, 50_000 to 2, 100_000 to 3, 125_000 to 4,
      250_000 to 5, 500_000 to 6, 800_000 to 7, 1_000_000 to 8,
    )
    /** Scan order: speeds HMG M-CAN is known to use first. */
    val SCAN_ORDER = listOf(100_000, 500_000, 125_000, 250_000)
    private const val SCAN_WINDOW_MS = 4000L
    private const val SCAN_MIN_FRAMES = 20
    private const val SUMMARY_MS = 10_000L

    /** IDs whose every payload change is copied into the probe log. */
    val WATCH = mapOf(
      0x4BB to "TP_HU_TBT_CLU",
      0x4F4 to "TP_HU_ANDAUTO_CLU",
      0x4F5 to "TP_CLU_ANDAUTO_HU",
      0x4F2 to "TP_CLU_CARPLAY_HU",
      0x4F3 to "TP_HU_CARPLAY_CLU",
      0x444 to "NM_HUD",
      0x440 to "NM_H_U",
    )
  }

  @Volatile private var running = false
  private var thread: Thread? = null
  private var socket: Socket? = null

  private val counts = ConcurrentHashMap<Int, Long>()
  private val lastPayload = HashMap<Int, String>()
  @Volatile var totalFrames = 0L
    private set
  @Volatile var activeBitrate = 0
    private set
  @Volatile var dumpFile: File? = null
    private set

  fun isRunning() = running

  fun start(cfg: Config) {
    if (running) return
    running = true
    thread = Thread({ runLoop(cfg) }, "mcan-link").also { it.start() }
  }

  fun stop() {
    running = false
    try { socket?.close() } catch (_: Exception) {}
    thread?.join(2000)
    thread = null
  }

  fun countsSnapshot(): Map<Int, Long> = HashMap(counts)

  private fun status(msg: String) {
    ProbeLog.log("mcan_status", mapOf("msg" to msg))
    onStatus(msg)
  }

  private fun runLoop(cfg: Config) {
    var backoffMs = 2000L
    while (running) {
      try {
        session(cfg)
        backoffMs = 2000L
      } catch (e: StopException) {
        status("중지: ${e.message}")
        running = false
        break
      } catch (e: Exception) {
        if (!running) break
        status("연결 끊김: ${e.javaClass.simpleName} ${e.message ?: ""} (재시도 ${backoffMs / 1000}s)")
      } finally {
        try { socket?.close() } catch (_: Exception) {}
        socket = null
      }
      sleepWhileRunning(backoffMs)
      backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
    }
    status("정지됨")
  }

  private class StopException(msg: String) : Exception(msg)

  private fun session(cfg: Config) {
    status("연결 중 ${cfg.host}:${cfg.port}")
    val sock = Socket()
    sock.tcpNoDelay = true
    sock.connect(InetSocketAddress(cfg.host, cfg.port), 5000)
    sock.soTimeout = 1000
    socket = sock
    val input = sock.getInputStream()
    val output = sock.getOutputStream()
    val reader = LineReader(input)

    val bitrate = if (cfg.bitrate > 0) cfg.bitrate else scan(cfg, reader, output)
    open(cfg, reader, output, bitrate)
    activeBitrate = bitrate
    status("수신 중 ${bitrate / 1000}kbps (listen-only)")
    ProbeLog.log("mcan_open", mapOf("host" to cfg.host, "port" to cfg.port, "bitrate" to bitrate))

    openDump(bitrate).use { dump ->
      var lastSummary = System.currentTimeMillis()
      while (running) {
        val line = reader.readLine() ?: continue
        handleLine(line, dump)
        val now = System.currentTimeMillis()
        if (now - lastSummary >= SUMMARY_MS) {
          lastSummary = now
          dump.flush()
          logSummary()
        }
      }
    }
    try { output.write("C\r".toByteArray()) } catch (_: Exception) {}
  }

  /** Try each speed listen-only and keep the first one that yields clean frames. */
  private fun scan(cfg: Config, reader: LineReader, output: OutputStream): Int {
    for (rate in SCAN_ORDER) {
      if (!running) throw StopException("사용자 중지")
      status("속도 탐색 ${rate / 1000}kbps")
      open(cfg, reader, output, rate)
      val deadline = System.currentTimeMillis() + SCAN_WINDOW_MS
      var good = 0
      while (running && System.currentTimeMillis() < deadline) {
        val line = reader.readLine() ?: continue
        if (parseFrame(line) != null) good++
      }
      ProbeLog.log("mcan_scan", mapOf("bitrate" to rate, "frames" to good))
      output.write("C\r".toByteArray())
      output.flush()
      drain(reader, 300)
      if (good >= SCAN_MIN_FRAMES) return rate
    }
    throw StopException("어떤 속도에서도 프레임이 없습니다. 배선/전원(ACC)/어댑터 모드를 확인하세요")
  }

  private fun open(cfg: Config, reader: LineReader, output: OutputStream, bitrate: Int) {
    val code = BITRATES[bitrate] ?: throw StopException("지원하지 않는 속도 $bitrate")
    command(reader, output, "C")  // close quietly; may fail when already closed
    if (!command(reader, output, "S$code")) throw StopException("어댑터가 속도 설정(S$code)을 거부했습니다")
    if (command(reader, output, "L")) return
    if (cfg.adapterSilent) {
      status("어댑터가 L을 거부함 → 어댑터 자체 무음 설정을 믿고 O로 엽니다")
      if (command(reader, output, "O")) return
    }
    throw StopException("어댑터가 listen-only(L)를 지원하지 않습니다. 어댑터를 무음(silent) 모드로 설정한 뒤 '어댑터 무음 설정됨'을 켜세요")
  }

  /** Send an SLCAN command and wait for CR (ok) or BEL (error). Frames arriving meanwhile are ignored. */
  private fun command(reader: LineReader, output: OutputStream, cmd: String): Boolean {
    output.write("$cmd\r".toByteArray())
    output.flush()
    val deadline = System.currentTimeMillis() + 800
    while (System.currentTimeMillis() < deadline) {
      val r = reader.readLine() ?: continue
      if (r == LineReader.BELL) return false
      if (r.isEmpty() || r == "z" || r == "Z") return true
    }
    // Some TCP bridges do not echo acknowledgements; treat silence as accepted.
    return true
  }

  private fun drain(reader: LineReader, ms: Long) {
    val deadline = System.currentTimeMillis() + ms
    while (System.currentTimeMillis() < deadline) reader.readLine()
  }

  private fun openDump(bitrate: Int): BufferedWriter {
    val dir = context.getExternalFilesDir(null) ?: context.filesDir
    val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
    val file = File(dir, "mcan-$stamp-${bitrate / 1000}k.log")
    dumpFile = file
    ProbeLog.log("mcan_dump", mapOf("file" to file.absolutePath))
    return BufferedWriter(FileWriter(file, true), 64 * 1024)
  }

  data class Frame(val id: Int, val ext: Boolean, val rtr: Boolean, val data: String)

  private fun handleLine(line: String, dump: BufferedWriter) {
    val f = parseFrame(line) ?: return
    totalFrames++
    counts.merge(f.id, 1L, Long::plus)
    val now = System.currentTimeMillis()
    val idHex = if (f.ext) "%08X".format(f.id) else "%03X".format(f.id)
    dump.write("(%d.%03d) mcan %s#%s%s\n".format(now / 1000, now % 1000, idHex, if (f.rtr) "R" else "", f.data))
    val name = WATCH[f.id] ?: return
    if (f.ext) return
    if (lastPayload.put(f.id, f.data) != f.data) {
      ProbeLog.log("mcan_frame", mapOf("id" to idHex, "name" to name, "data" to f.data))
    }
  }

  private fun logSummary() {
    val top = counts.entries.sortedBy { it.key }.associate { "%03X".format(it.key) to it.value }
    ProbeLog.log("mcan_summary", mapOf("bitrate" to activeBitrate, "total" to totalFrames, "ids" to top))
  }

  /** Parse "tIIILDD..[TTTT]", "TIIIIIIIILDD..[TTTT]", "rIIIL", "RIIIIIIIIL". */
  fun parseFrame(line: String): Frame? {
    if (line.isEmpty()) return null
    val kind = line[0]
    val ext = kind == 'T' || kind == 'R'
    val rtr = kind == 'r' || kind == 'R'
    if (kind != 't' && kind != 'T' && kind != 'r' && kind != 'R') return null
    val idLen = if (ext) 8 else 3
    if (line.length < 1 + idLen + 1) return null
    val id = line.substring(1, 1 + idLen).toIntOrNull(16) ?: return null
    val dlc = line[1 + idLen].digitToIntOrNull(16) ?: return null
    if (dlc > 8) return null
    val dataStart = 2 + idLen
    val data = if (rtr) "" else {
      if (line.length < dataStart + dlc * 2) return null
      line.substring(dataStart, dataStart + dlc * 2).uppercase()
    }
    if (data.any { it.digitToIntOrNull(16) == null }) return null
    return Frame(id, ext, rtr, data)
  }

  private fun sleepWhileRunning(ms: Long) {
    val end = System.currentTimeMillis() + ms
    while (running && System.currentTimeMillis() < end) Thread.sleep(100)
  }

  /** CR-terminated reader; returns null on socket timeout, BELL for an error byte. */
  private class LineReader(private val input: InputStream) {
    companion object { const val BELL = "\u0007" }
    private val buf = StringBuilder()

    fun readLine(): String? {
      while (true) {
        val b = try {
          input.read()
        } catch (_: java.net.SocketTimeoutException) {
          return null
        }
        if (b < 0) throw java.io.EOFException("adapter closed connection")
        when (b) {
          0x07 -> { buf.setLength(0); return BELL }
          0x0D, 0x0A -> {
            if (b == 0x0A && buf.isEmpty()) continue
            val s = buf.toString()
            buf.setLength(0)
            return s
          }
          else -> if (buf.length < 64) buf.append(b.toChar())
        }
      }
    }
  }
}
