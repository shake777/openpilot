package com.carrot.aaprobe

import android.util.Base64
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest

/** Minimal RFC 6455 helpers for the Carrot Navi v2 sockets (text/binary, ping/pong, close). */
object WebSocketIo {
  const val OP_CONT = 0
  const val OP_TEXT = 1
  const val OP_BINARY = 2
  const val OP_CLOSE = 8
  const val OP_PING = 9
  const val OP_PONG = 10
  private const val GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
  private const val MAX_MESSAGE = 8 * 1024 * 1024 + 64

  data class Message(val opcode: Int, val payload: ByteArray) {
    fun text() = String(payload, Charsets.UTF_8)
  }

  class Request(val head: ByteArray, val method: String, val path: String, val headers: Map<String, String>)

  /** Read an HTTP request head up to and including CRLFCRLF. */
  fun readRequest(input: InputStream): Request {
    val buf = ByteArrayOutputStream()
    var match = 0
    while (true) {
      val b = input.read()
      if (b < 0) throw EOFException("closed before request head")
      buf.write(b)
      match = when {
        (match == 0 || match == 2) && b == 0x0D -> match + 1
        (match == 1 || match == 3) && b == 0x0A -> match + 1
        b == 0x0D -> 1
        else -> 0
      }
      if (match == 4) break
      if (buf.size() > 16 * 1024) throw IllegalStateException("request head too large")
    }
    val head = buf.toByteArray()
    val lines = String(head, Charsets.ISO_8859_1).split("\r\n")
    val parts = lines.first().split(" ")
    val headers = HashMap<String, String>()
    for (line in lines.drop(1)) {
      val i = line.indexOf(':')
      if (i > 0) headers[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
    }
    return Request(head, parts.getOrElse(0) { "" }, parts.getOrElse(1) { "" }, headers)
  }

  fun acceptResponse(req: Request): ByteArray {
    val key = req.headers["sec-websocket-key"] ?: throw IllegalStateException("missing websocket key")
    val accept = Base64.encodeToString(
      MessageDigest.getInstance("SHA-1").digest((key + GUID).toByteArray(Charsets.ISO_8859_1)),
      Base64.NO_WRAP,
    )
    return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
      "Sec-WebSocket-Accept: $accept\r\n\r\n").toByteArray(Charsets.ISO_8859_1)
  }

  /** Read one complete message, joining fragments. Control frames are returned as they arrive. */
  fun readMessage(input: InputStream): Message {
    var opcode = -1
    val data = ByteArrayOutputStream()
    while (true) {
      val b0 = readByte(input)
      val b1 = readByte(input)
      val fin = b0 and 0x80 != 0
      val op = b0 and 0x0F
      val masked = b1 and 0x80 != 0
      var len = (b1 and 0x7F).toLong()
      if (len == 126L) len = ((readByte(input) shl 8) or readByte(input)).toLong()
      else if (len == 127L) {
        len = 0
        repeat(8) { len = (len shl 8) or readByte(input).toLong() }
      }
      if (len < 0 || len > MAX_MESSAGE) throw IllegalStateException("frame too large: $len")
      val mask = if (masked) ByteArray(4).also { readFully(input, it) } else null
      val payload = ByteArray(len.toInt())
      readFully(input, payload)
      if (mask != null) for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i and 3].toInt()).toByte()
      if (op >= OP_CLOSE) return Message(op, payload)
      if (op != OP_CONT) opcode = op
      data.write(payload)
      if (data.size() > MAX_MESSAGE) throw IllegalStateException("message too large")
      if (fin) return Message(if (opcode < 0) OP_BINARY else opcode, data.toByteArray())
    }
  }

  /** Write one unmasked (server-side) frame. */
  fun writeFrame(out: OutputStream, opcode: Int, payload: ByteArray) {
    synchronized(out) {
      out.write(0x80 or opcode)
      when {
        payload.size < 126 -> out.write(payload.size)
        payload.size < 65536 -> {
          out.write(126); out.write(payload.size shr 8); out.write(payload.size and 0xFF)
        }
        else -> {
          out.write(127)
          for (shift in 56 downTo 0 step 8) out.write(((payload.size.toLong() shr shift) and 0xFF).toInt())
        }
      }
      out.write(payload)
      out.flush()
    }
  }

  private fun readByte(input: InputStream): Int {
    val b = input.read()
    if (b < 0) throw EOFException("websocket closed")
    return b
  }

  private fun readFully(input: InputStream, buf: ByteArray) {
    var off = 0
    while (off < buf.size) {
      val n = input.read(buf, off, buf.size - off)
      if (n < 0) throw EOFException("websocket closed")
      off += n
    }
  }
}
