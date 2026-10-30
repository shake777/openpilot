package com.carrot.aaprobe

import android.content.Intent
import androidx.car.app.CarAppService
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.validation.HostValidator

class ProbeCarAppService : CarAppService() {
  // Sideloaded diagnostic tool: accept any host (Android Auto, DHU).
  override fun createHostValidator(): HostValidator = HostValidator.ALLOW_ALL_HOSTS_VALIDATOR

  override fun onCreateSession(): Session {
    ProbeLog.init(applicationContext)
    return ProbeSession()
  }
}

class ProbeSession : Session() {
  override fun onCreateScreen(intent: Intent): Screen {
    ProbeLog.log("session_start", mapOf("intent" to intent.action))
    return ProbeScreen(carContext)
  }
}
