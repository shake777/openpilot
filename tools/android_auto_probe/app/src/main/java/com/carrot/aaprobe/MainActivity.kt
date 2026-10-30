package com.carrot.aaprobe

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import android.text.InputType
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** Phone-side settings: permissions, optional UDP log target, and the latest log lines. */
class MainActivity : Activity() {
  private val permissions = arrayOf(
    Manifest.permission.ACCESS_FINE_LOCATION,
    Manifest.permission.ACCESS_COARSE_LOCATION,
    "com.google.android.gms.permission.CAR_SPEED",
    "com.google.android.gms.permission.CAR_MILEAGE",
    "com.google.android.gms.permission.CAR_FUEL",
  )

  private lateinit var status: TextView
  private lateinit var logView: TextView

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
      text = "AA Probe\n안드로이드 오토에서 'AA Probe'를 실행하면 헤드유닛 데이터 수집과 가짜 TBT 시험을 합니다."
      textSize = 16f
    })

    status = TextView(this).apply { setPadding(0, pad, 0, pad) }
    root.addView(status)

    root.addView(Button(this).apply {
      text = "권한 요청"
      setOnClickListener { requestPermissions(permissions, 1) }
    })

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
