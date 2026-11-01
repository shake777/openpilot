package com.carrot.aaprobe

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.ArrayAdapter
import android.widget.TextView
import android.widget.Toast

/**
 * Phone-side settings: permissions, optional UDP log target, the receive-only M-CAN recorder,
 * TMAP/Naver guidance-notification capture, and the latest log lines.
 */
class MainActivity : Activity() {
  private val permissions = arrayOf(
    Manifest.permission.ACCESS_FINE_LOCATION,
    Manifest.permission.ACCESS_COARSE_LOCATION,
    "com.google.android.gms.permission.CAR_SPEED",
    "com.google.android.gms.permission.CAR_MILEAGE",
    "com.google.android.gms.permission.CAR_FUEL",
  )

  private lateinit var status: TextView
  private lateinit var mcanStatus: TextView
  private lateinit var bridgeStatus: TextView
  private lateinit var logView: TextView
  private val ui = Handler(Looper.getMainLooper())
  private val ticker = object : Runnable {
    override fun run() {
      refreshMcan()
      ui.postDelayed(this, 1000)
    }
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    ProbeLog.init(applicationContext)
    val prefs = getSharedPreferences(ProbeLog.PREFS, Context.MODE_PRIVATE)
    val pad = (16 * resources.displayMetrics.density).toInt()

    val root = LinearLayout(this).apply {
      orientation = LinearLayout.VERTICAL
      setPadding(pad, pad, pad, pad)
    }
    root.addView(TextView(this).apply {
      text = "AA Probe\n① 안드로이드 오토: TMAP·네이버 안내 중계, 헤드유닛 데이터 수집, 가짜 TBT 시험\n② 미러링 연결: M-CAN 수신 기록 + TMAP·네이버 안내 알림 기록"
      textSize = 16f
    })

    status = TextView(this).apply { setPadding(0, pad, 0, pad) }
    root.addView(status)

    root.addView(Button(this).apply {
      text = "권한 요청"
      setOnClickListener {
        val all = if (Build.VERSION.SDK_INT >= 33) permissions + Manifest.permission.POST_NOTIFICATIONS else permissions
        requestPermissions(all, 1)
      }
    })
    root.addView(Button(this).apply {
      text = "알림 접근 허용 (TMAP/네이버 안내 기록)"
      setOnClickListener { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
    })

    root.addView(TextView(this).apply {
      text = "\nM-CAN 수신 (SLCAN-over-TCP 어댑터, 예: WiCAN slcan 모드)\n차량에 아무것도 보내지 않습니다(listen-only)."
      textSize = 15f
    })
    val mcanHost = EditText(this).apply {
      hint = "어댑터 IP (WiCAN 기본 192.168.80.1)"
      setText(prefs.getString(McanService.PREF_HOST, McanService.DEFAULT_HOST))
      inputType = InputType.TYPE_CLASS_TEXT
    }
    val mcanPort = EditText(this).apply {
      hint = "포트 (WiCAN 기본 3333)"
      setText(prefs.getInt(McanService.PREF_PORT, McanService.DEFAULT_PORT).toString())
      inputType = InputType.TYPE_CLASS_NUMBER
    }
    val rateLabels = listOf("자동 탐색") + McanLink.BITRATES.keys.map { "${it / 1000} kbps" }
    val rateValues = listOf(0) + McanLink.BITRATES.keys.toList()
    val mcanRate = Spinner(this).apply {
      adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, rateLabels)
      setSelection(rateValues.indexOf(prefs.getInt(McanService.PREF_BITRATE, 0)).coerceAtLeast(0))
    }
    val mcanSilent = CheckBox(this).apply {
      text = "어댑터 무음(silent) 설정됨 — L 명령 미지원 어댑터일 때만"
      isChecked = prefs.getBoolean(McanService.PREF_SILENT, false)
    }
    root.addView(mcanHost, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
    root.addView(mcanPort, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
    root.addView(mcanRate)
    root.addView(mcanSilent)
    root.addView(Button(this).apply {
      text = "M-CAN 기록 시작/중지"
      setOnClickListener {
        if (McanService.link?.isRunning() == true) {
          McanService.stop(this@MainActivity)
        } else {
          prefs.edit()
            .putString(McanService.PREF_HOST, mcanHost.text.toString().trim())
            .putInt(McanService.PREF_PORT, mcanPort.text.toString().toIntOrNull() ?: McanService.DEFAULT_PORT)
            .putInt(McanService.PREF_BITRATE, rateValues[mcanRate.selectedItemPosition])
            .putBoolean(McanService.PREF_SILENT, mcanSilent.isChecked)
            .apply()
          McanService.start(this@MainActivity)
        }
        ui.postDelayed({ refreshMcan() }, 300)
      }
    })
    mcanStatus = TextView(this).apply { setPadding(0, pad / 2, 0, pad) }
    root.addView(mcanStatus)

    root.addView(TextView(this).apply {
      text = "\n내비 중계 (TMAP·네이버 → 안드로이드 오토 → 계기판/HUD)\nTMAP·네이버가 휴대폰의 AA Probe로 연결하고, AA Probe가 같은 데이터를 C4로 그대로 넘깁니다."
      textSize = 15f
    })
    val modeLabels = listOf("C4 자동(Wi-Fi 게이트웨이)", "C4 주소 직접 입력", "C4 없음(단독)")
    val modeValues = listOf("auto", "manual", "none")
    val upstreamMode = Spinner(this).apply {
      adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, modeLabels)
      setSelection(modeValues.indexOf(prefs.getString(NaviBridgeService.PREF_UPSTREAM_MODE, "auto")).coerceAtLeast(0))
    }
    val upstreamHost = EditText(this).apply {
      hint = "C4 IP (직접 입력일 때)"
      setText(prefs.getString(NaviBridgeService.PREF_UPSTREAM_HOST, ""))
      inputType = InputType.TYPE_CLASS_TEXT
    }
    root.addView(upstreamMode)
    root.addView(upstreamHost, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
    root.addView(Button(this).apply {
      text = "내비 중계 시작/중지"
      setOnClickListener {
        if (NaviBridgeService.bridge?.isRunning() == true) {
          NaviBridgeService.stop(this@MainActivity)
        } else {
          prefs.edit()
            .putString(NaviBridgeService.PREF_UPSTREAM_MODE, modeValues[upstreamMode.selectedItemPosition])
            .putString(NaviBridgeService.PREF_UPSTREAM_HOST, upstreamHost.text.toString().trim())
            .apply()
          NaviBridgeService.start(this@MainActivity)
        }
        ui.postDelayed({ refreshMcan() }, 300)
      }
    })
    bridgeStatus = TextView(this).apply { setPadding(0, pad / 2, 0, pad) }
    root.addView(bridgeStatus)

    val udpEnabled = CheckBox(this).apply {
      text = "로그를 UDP로도 전송"
      isChecked = prefs.getBoolean(ProbeLog.PREF_UDP_ENABLED, false)
    }
    val host = EditText(this).apply {
      hint = "수신 IP (예: C4 또는 PC 주소, 255.255.255.255 = 브로드캐스트)"
      setText(prefs.getString(ProbeLog.PREF_UDP_HOST, ""))
      inputType = InputType.TYPE_CLASS_TEXT
    }
    val port = EditText(this).apply {
      hint = "포트"
      setText(prefs.getInt(ProbeLog.PREF_UDP_PORT, ProbeLog.DEFAULT_UDP_PORT).toString())
      inputType = InputType.TYPE_CLASS_NUMBER
    }
    root.addView(udpEnabled)
    root.addView(host, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
    root.addView(port, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
    root.addView(Button(this).apply {
      text = "UDP 설정 저장"
      setOnClickListener {
        prefs.edit()
          .putBoolean(ProbeLog.PREF_UDP_ENABLED, udpEnabled.isChecked)
          .putString(ProbeLog.PREF_UDP_HOST, host.text.toString().trim())
          .putInt(ProbeLog.PREF_UDP_PORT, port.text.toString().toIntOrNull() ?: ProbeLog.DEFAULT_UDP_PORT)
          .apply()
        ProbeLog.reloadUdp(applicationContext)
        ProbeLog.log("udp_config", mapOf("enabled" to udpEnabled.isChecked, "host" to host.text.toString().trim()))
        Toast.makeText(this@MainActivity, "저장됨", Toast.LENGTH_SHORT).show()
      }
    })
    root.addView(Button(this).apply {
      text = "최근 로그 새로고침"
      setOnClickListener { refresh() }
    })

    logView = TextView(this).apply {
      textSize = 11f
      setTextIsSelectable(true)
      setPadding(0, pad, 0, 0)
    }
    root.addView(logView)

    setContentView(ScrollView(this).apply { addView(root) })
  }

  override fun onResume() {
    super.onResume()
    refresh()
    ui.post(ticker)
  }

  override fun onPause() {
    super.onPause()
    ui.removeCallbacks(ticker)
  }

  private fun notificationAccess(): Boolean {
    val enabled = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: return false
    val me = ComponentName(this, NavNotificationListener::class.java).flattenToString()
    return enabled.split(':').any { it == me }
  }

  private fun refreshMcan() {
    val l = McanService.link
    val counts = l?.countsSnapshot().orEmpty()
    val watched = McanLink.WATCH.entries.joinToString("  ") { (id, _) -> "%03X:%d".format(id, counts[id] ?: 0) }
    mcanStatus.text = buildString {
      append("상태: ${McanService.lastStatus}\n")
      if (l != null) append("프레임 ${l.totalFrames}, ID ${counts.size}개\n$watched\n파일: ${l.dumpFile?.absolutePath ?: "-"}\n")
      append("알림 접근: ${if (notificationAccess()) "허용됨" else "꺼짐"}  마지막 안내: ${NavNotificationListener.lastSummary.ifBlank { "-" }}")
    }
    val b = NaviBridge.state
    val (up, upPort) = NaviBridgeService.upstream(this)
    bridgeStatus.text = buildString {
      append("상태: ${NaviBridgeService.lastStatus}\n")
      append("C4: ${up?.let { "$it:$upPort" } ?: "없음(단독)"}  연결 ${b.connections}  보낸 앱 ${b.sender.ifBlank { "-" }}\n")
      val cur = b.current
      if (cur == null) append("현재 안내: 없음")
      else append("현재 안내: ${TurnMap.of(cur.turnType).label} ${cur.distanceM}m (code ${cur.turnType}) ${cur.roadName}")
      b.next?.let { append("\n다음 안내: ${TurnMap.of(it.turnType).label} ${it.distanceM}m (code ${it.turnType})") }
    }
  }

  override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
    super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    refresh()
  }

  private fun refresh() {
    val perms = permissions.joinToString("\n") { p ->
      val ok = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
      "${if (ok) "✔" else "✘"} ${p.substringAfterLast('.')}"
    }
    status.text = "로그 파일: ${ProbeLog.logFile?.absolutePath}\n\n권한\n$perms"
    logView.text = ProbeLog.recentLines(60).reversed().joinToString("\n")
  }
}
