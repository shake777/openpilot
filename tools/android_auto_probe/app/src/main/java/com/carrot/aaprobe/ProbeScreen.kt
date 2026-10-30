package com.carrot.aaprobe

import android.Manifest
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import androidx.car.app.CarContext
import androidx.car.app.CarToast
import androidx.car.app.Screen
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.CarIcon
import androidx.car.app.model.DateTimeWithZone
import androidx.car.app.model.Distance
import androidx.car.app.model.Template
import androidx.car.app.navigation.NavigationManager
import androidx.car.app.navigation.NavigationManagerCallback
import androidx.car.app.navigation.model.Destination
import androidx.car.app.navigation.model.Maneuver
import androidx.car.app.navigation.model.NavigationTemplate
import androidx.car.app.navigation.model.RoutingInfo
import androidx.car.app.navigation.model.Step
import androidx.car.app.navigation.model.TravelEstimate
import androidx.car.app.navigation.model.Trip
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.IconCompat
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import java.time.ZonedDateTime

/**
 * One screen that (1) shows/logs head-unit hardware data and (2) runs a fake turn-by-turn
 * route through NavigationManager.updateTrip so the cluster/HUD can be watched for arrows.
 */
class ProbeScreen(carContext: CarContext) : Screen(carContext), DefaultLifecycleObserver {
  private data class TestStep(val type: Int, val cue: String, val road: String)

  private val steps = listOf(
    TestStep(Maneuver.TYPE_TURN_NORMAL_RIGHT, "우회전", "프로브1로"),
    TestStep(Maneuver.TYPE_TURN_NORMAL_LEFT, "좌회전", "프로브2로"),
    TestStep(Maneuver.TYPE_STRAIGHT, "직진", "프로브3로"),
    TestStep(Maneuver.TYPE_TURN_SLIGHT_RIGHT, "오른쪽 방향", "프로브4로"),
    TestStep(Maneuver.TYPE_KEEP_LEFT, "왼쪽 유지", "프로브5로"),
    TestStep(Maneuver.TYPE_U_TURN_LEFT, "유턴", "프로브6로"),
    TestStep(Maneuver.TYPE_DESTINATION, "목적지 도착", "프로브 목적지"),
  )

  private val stepStartM = 500.0
  private val stepRateMps = 25.0  // 20 s per step

  private val handler = Handler(Looper.getMainLooper())
  private val navigationManager = carContext.getCarService(NavigationManager::class.java)
  private val renderer = SurfaceRenderer(carContext) { overlayLines() }
  private val sensors = SensorProbe(carContext) { renderer.requestRender() }
  private val maneuverIcon = CarIcon.Builder(IconCompat.createWithResource(carContext, R.drawable.ic_maneuver)).build()

  private var navigating = false
  private var stepIndex = 0
  private var distanceM = stepStartM
  private var lastTripError: String? = null

  private val tick = object : Runnable {
    override fun run() {
      if (!navigating) return
      distanceM -= stepRateMps
      if (distanceM <= 0.0) advanceStep() else pushTrip()
      handler.postDelayed(this, 1000)
    }
  }

  init {
    lifecycle.addObserver(this)
  }

  override fun onCreate(owner: LifecycleOwner) {
    navigationManager.setNavigationManagerCallback(object : NavigationManagerCallback {
      override fun onStopNavigation() {
        ProbeLog.log("nav_host_stop")
        stopNavigation(notifyHost = false)
      }
    })
    requestPermissionsThenStart()
  }

  override fun onDestroy(owner: LifecycleOwner) {
    stopNavigation(notifyHost = true)
    sensors.stop()
    handler.removeCallbacksAndMessages(null)
  }

  override fun onGetTemplate(): Template {
    val strip = ActionStrip.Builder()
      .addAction(Action.Builder()
        .setTitle(if (navigating) "TBT 중지" else "TBT 시작")
        .setOnClickListener { if (navigating) stopNavigation(notifyHost = true) else startNavigation() }
        .build())
      .addAction(Action.Builder()
        .setTitle("다음 안내")
        .setOnClickListener { if (navigating) advanceStep() }
        .build())
      .addAction(Action.Builder()
        .setTitle("센서 재시작")
        .setOnClickListener { sensors.restart() }
        .build())
      .build()

    val builder = NavigationTemplate.Builder().setActionStrip(strip)
    if (navigating) {
      builder.setNavigationInfo(RoutingInfo.Builder()
        .setCurrentStep(currentStep(), Distance.create(distanceM, Distance.UNIT_METERS))
        .build())
      builder.setDestinationTravelEstimate(destinationEstimate())
    }
    return builder.build()
  }

  private fun requestPermissionsThenStart() {
    val wanted = listOf(
      Manifest.permission.ACCESS_FINE_LOCATION,
      "com.google.android.gms.permission.CAR_SPEED",
      "com.google.android.gms.permission.CAR_MILEAGE",
      "com.google.android.gms.permission.CAR_FUEL",
    )
    val missing = wanted.filter {
      ContextCompat.checkSelfPermission(carContext, it) != PackageManager.PERMISSION_GRANTED
    }
    ProbeLog.log("permissions", mapOf("missing" to missing))
    if (missing.isEmpty()) {
      sensors.start()
      return
    }
    try {
      carContext.requestPermissions(missing) { granted, rejected ->
        ProbeLog.log("permissions_result", mapOf("granted" to granted, "rejected" to rejected))
        sensors.start()
      }
    } catch (e: Exception) {
      ProbeLog.log("permissions_result", mapOf("error" to "${e.javaClass.simpleName}: ${e.message}"))
      sensors.start()
    }
  }

  private fun startNavigation() {
    try {
      navigationManager.navigationStarted()
    } catch (e: Exception) {
      lastTripError = "${e.javaClass.simpleName}: ${e.message}"
      ProbeLog.log("nav_start", mapOf("ok" to false, "error" to lastTripError))
      CarToast.makeText(carContext, "내비 시작 실패", CarToast.LENGTH_LONG).show()
      return
    }
    navigating = true
    stepIndex = 0
    distanceM = stepStartM
    ProbeLog.log("nav_start", mapOf("ok" to true))
    pushTrip()
    handler.removeCallbacks(tick)
    handler.postDelayed(tick, 1000)
  }

  private fun stopNavigation(notifyHost: Boolean) {
    if (!navigating) return
    navigating = false
    handler.removeCallbacks(tick)
    if (notifyHost) {
      try {
        navigationManager.navigationEnded()
      } catch (e: Exception) {
        ProbeLog.log("nav_end", mapOf("ok" to false, "error" to "${e.javaClass.simpleName}: ${e.message}"))
      }
    }
    ProbeLog.log("nav_end", mapOf("ok" to true))
    invalidate()
    renderer.requestRender()
  }

  private fun advanceStep() {
    stepIndex = (stepIndex + 1) % steps.size
    distanceM = stepStartM
    pushTrip()
  }

  private fun currentStep(): Step {
    val s = steps[stepIndex]
    return Step.Builder(s.cue)
      .setManeuver(Maneuver.Builder(s.type).setIcon(maneuverIcon).build())
      .setRoad(s.road)
      .build()
  }

  private fun remainingTotalM(): Double = distanceM + (steps.size - 1 - stepIndex) * stepStartM

  private fun estimate(meters: Double): TravelEstimate {
    val seconds = (meters / stepRateMps).toLong()
    return TravelEstimate.Builder(
      Distance.create(meters, Distance.UNIT_METERS),
      DateTimeWithZone.create(ZonedDateTime.now().plusSeconds(seconds)),
    ).setRemainingTimeSeconds(seconds).build()
  }

  private fun destinationEstimate(): TravelEstimate = estimate(remainingTotalM())

  private fun pushTrip() {
    val s = steps[stepIndex]
    val trip = Trip.Builder()
      .addStep(currentStep(), estimate(distanceM))
      .addDestination(Destination.Builder().setName("AA Probe 시험").setAddress("가짜 경로").build(), destinationEstimate())
      .setCurrentRoad("AA Probe 시험로")
      .build()
    try {
      navigationManager.updateTrip(trip)
      lastTripError = null
      ProbeLog.log("tbt", mapOf("step" to stepIndex, "maneuver_type" to s.type, "cue" to s.cue, "distance_m" to distanceM))
    } catch (e: Exception) {
      lastTripError = "${e.javaClass.simpleName}: ${e.message}"
      ProbeLog.log("tbt", mapOf("step" to stepIndex, "ok" to false, "error" to lastTripError))
    }
    invalidate()
    renderer.requestRender()
  }

  private fun overlayLines(): List<String> {
    val header = mutableListOf("AA Probe  api=${carContext.carAppApiLevel}")
    header += if (navigating) {
      val s = steps[stepIndex]
      "TBT ON  #$stepIndex ${s.cue} ${distanceM.toInt()}m  (type ${s.type})"
    } else {
      "TBT OFF  (TBT 시작 버튼으로 시험)"
    }
    lastTripError?.let { header += "TBT 오류: $it" }
    return header + sensors.summaryLines()
  }
}
