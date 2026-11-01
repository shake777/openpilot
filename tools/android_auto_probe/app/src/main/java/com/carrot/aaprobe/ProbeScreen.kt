package com.carrot.aaprobe

import android.Manifest
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
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
 * One screen that (1) shows/logs head-unit hardware data, (2) runs a fake turn-by-turn route, and
 * (3) relays live TMAP/Naver guidance from the phone-local Carrot Navi v2 bridge, both through
 * NavigationManager.updateTrip so the cluster/HUD can be watched for arrows.
 */
class ProbeScreen(carContext: CarContext) : Screen(carContext), DefaultLifecycleObserver {
  private enum class Mode { NONE, FAKE, RELAY }

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
  /** Relay keeps the trip alive this long after guidance stops, then ends navigation. */
  private val relayStaleMs = 10_000L
  /** Upper bound on updateTrip calls while relaying (the host throttles further). */
  private val relayMinIntervalMs = 500L

  private val handler = Handler(Looper.getMainLooper())
  private val navigationManager = carContext.getCarService(NavigationManager::class.java)
  private val renderer = SurfaceRenderer(carContext) { overlayLines() }
  private val sensors = SensorProbe(carContext) { renderer.requestRender() }
  private val maneuverIcon = CarIcon.Builder(IconCompat.createWithResource(carContext, R.drawable.ic_maneuver)).build()

  // The regular navigation app already sends TBT over Android Auto. Starting the fake route or the
  // relay makes the host stop that app's guidance, so it needs a second press within the window.
  private val confirmWindowMs = 3000L
  private var confirmUntil = 0L
  private var confirmFor = Mode.NONE

  private var mode = Mode.NONE
  private var relayArmed = false
  private var stepIndex = 0
  private var distanceM = stepStartM
  private var lastTripError: String? = null
  private var lastRelayPush = 0L
  private var lastRelaySig = ""

  private val tick = object : Runnable {
    override fun run() {
      if (mode != Mode.FAKE) return
      distanceM -= stepRateMps
      if (distanceM <= 0.0) advanceStep() else pushTrip()
      handler.postDelayed(this, 1000)
    }
  }

  private val bridgeListener: (NaviBridge.State) -> Unit = { handler.post { onBridge(it) } }
  private val relayWatch = object : Runnable {
    override fun run() {
      if (!relayArmed) return
      onBridge(NaviBridge.state)
      handler.postDelayed(this, 1000)
    }
  }

  init {
    lifecycle.addObserver(this)
  }

  override fun onCreate(owner: LifecycleOwner) {
    navigationManager.setNavigationManagerCallback(object : NavigationManagerCallback {
      override fun onStopNavigation() {
        ProbeLog.log("nav_host_stop", mapOf("mode" to mode.name))
        relayArmed = false
        stopNavigation(notifyHost = false)
      }
    })
    NaviBridge.addListener(bridgeListener)
    requestPermissionsThenStart()
  }

  // Lifecycle is logged so the log shows whether sensor updates keep arriving while the
  // regular navigation app is in front (sensors stay subscribed until onDestroy).
  override fun onStart(owner: LifecycleOwner) = ProbeLog.log("lifecycle", mapOf("state" to "start"))
  override fun onResume(owner: LifecycleOwner) = ProbeLog.log("lifecycle", mapOf("state" to "resume"))
  override fun onPause(owner: LifecycleOwner) = ProbeLog.log("lifecycle", mapOf("state" to "pause"))
  override fun onStop(owner: LifecycleOwner) = ProbeLog.log("lifecycle", mapOf("state" to "stop"))

  override fun onDestroy(owner: LifecycleOwner) {
    ProbeLog.log("lifecycle", mapOf("state" to "destroy"))
    NaviBridge.removeListener(bridgeListener)
    relayArmed = false
    stopNavigation(notifyHost = true)
    sensors.stop()
    handler.removeCallbacksAndMessages(null)
  }

  override fun onGetTemplate(): Template {
    val strip = ActionStrip.Builder()
      .addAction(Action.Builder()
        .setTitle(when {
          relayArmed -> "중계 중지"
          confirmPending(Mode.RELAY) -> "한번 더: 중계"
          else -> "내비 중계"
        })
        .setOnClickListener { onRelayButton() }
        .build())
      .addAction(Action.Builder()
        .setTitle(when {
          mode == Mode.FAKE -> "TBT 중지"
          confirmPending(Mode.FAKE) -> "한번 더: 시작"
          else -> "TBT 시험"
        })
        .setOnClickListener { onTbtButton() }
        .build())
      .addAction(Action.Builder()
        .setTitle("다음 안내")
        .setOnClickListener { if (mode == Mode.FAKE) advanceStep() }
        .build())
      .addAction(Action.Builder()
        .setTitle("센서 재시작")
        .setOnClickListener { sensors.restart() }
        .build())
      .build()

    val builder = NavigationTemplate.Builder().setActionStrip(strip)
    when (mode) {
      Mode.FAKE -> {
        builder.setNavigationInfo(RoutingInfo.Builder()
          .setCurrentStep(fakeStep(), Distance.create(distanceM, Distance.UNIT_METERS))
          .build())
        builder.setDestinationTravelEstimate(estimate(remainingTotalM(), stepRateMps))
      }
      Mode.RELAY -> {
        val s = NaviBridge.state
        val cur = s.current
        if (cur != null) {
          val info = RoutingInfo.Builder()
            .setCurrentStep(relayStep(cur), Distance.create(cur.distanceM.toDouble(), Distance.UNIT_METERS))
          s.next?.let { info.setNextStep(relayStep(it)) }
          builder.setNavigationInfo(info.build())
          relayDestination(s)?.let { builder.setDestinationTravelEstimate(it) }
        } else {
          builder.setNavigationInfo(RoutingInfo.Builder().setLoading(true).build())
        }
      }
      Mode.NONE -> {}
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

  private fun confirmPending(m: Mode): Boolean = confirmFor == m && SystemClock.elapsedRealtime() < confirmUntil

  private fun askConfirm(m: Mode, what: String) {
    confirmFor = m
    confirmUntil = SystemClock.elapsedRealtime() + confirmWindowMs
    CarToast.makeText(carContext, "실행 중인 내비 안내가 중단됩니다. 3초 안에 한 번 더 누르면 $what", CarToast.LENGTH_LONG).show()
    handler.postDelayed({ invalidate() }, confirmWindowMs + 100)
    invalidate()
  }

  private fun onTbtButton() {
    when {
      mode == Mode.FAKE -> stopNavigation(notifyHost = true)
      confirmPending(Mode.FAKE) -> {
        confirmUntil = 0L
        relayArmed = false
        if (mode == Mode.RELAY) stopNavigation(notifyHost = true)
        startFake()
      }
      else -> askConfirm(Mode.FAKE, "가짜 경로를 시작합니다")
    }
  }

  private fun onRelayButton() {
    when {
      relayArmed -> {
        relayArmed = false
        ProbeLog.log("relay", mapOf("state" to "off"))
        if (mode == Mode.RELAY) stopNavigation(notifyHost = true)
        invalidate()
      }
      confirmPending(Mode.RELAY) -> {
        confirmUntil = 0L
        if (mode == Mode.FAKE) stopNavigation(notifyHost = true)
        ensureBridge()
        relayArmed = true
        ProbeLog.log("relay", mapOf("state" to "armed"))
        handler.removeCallbacks(relayWatch)
        handler.post(relayWatch)
        invalidate()
      }
      else -> askConfirm(Mode.RELAY, "TMAP/네이버 안내를 중계합니다")
    }
  }

  private fun ensureBridge() {
    if (NaviBridgeService.bridge?.isRunning() == true) return
    try {
      NaviBridgeService.start(carContext)
    } catch (e: Exception) {
      ProbeLog.log("relay", mapOf("state" to "bridge_start_failed", "error" to "${e.javaClass.simpleName}: ${e.message}"))
      CarToast.makeText(carContext, "휴대폰의 AA Probe에서 '내비 중계 시작'을 먼저 눌러 주세요", CarToast.LENGTH_LONG).show()
    }
  }

  /** Start relaying when fresh guidance exists; end it once guidance has been gone for a while. */
  private fun onBridge(s: NaviBridge.State) {
    if (!relayArmed) return
    val fresh = s.current != null && s.guideAgeMs() < relayStaleMs
    if (mode != Mode.RELAY) {
      if (!fresh) return
      if (!beginNavigation()) return
      mode = Mode.RELAY
      lastRelaySig = ""
      ProbeLog.log("relay", mapOf("state" to "started", "sender" to s.sender))
    } else if (!fresh) {
      ProbeLog.log("relay", mapOf("state" to "guidance_gone"))
      stopNavigation(notifyHost = true)
      return
    }
    val cur = s.current ?: return
    val sig = "${cur.turnType}/${cur.distanceM}/${s.next?.turnType}/${s.remainM}"
    val now = SystemClock.elapsedRealtime()
    if (sig == lastRelaySig && now - lastRelayPush < 5000) return
    if (now - lastRelayPush < relayMinIntervalMs) return
    lastRelaySig = sig
    lastRelayPush = now
    pushRelayTrip(s, cur)
  }

  private fun beginNavigation(): Boolean = try {
    navigationManager.navigationStarted()
    ProbeLog.log("nav_start", mapOf("ok" to true))
    true
  } catch (e: Exception) {
    lastTripError = "${e.javaClass.simpleName}: ${e.message}"
    ProbeLog.log("nav_start", mapOf("ok" to false, "error" to lastTripError))
    CarToast.makeText(carContext, "내비 시작 실패", CarToast.LENGTH_LONG).show()
    false
  }

  private fun startFake() {
    if (!beginNavigation()) return
    mode = Mode.FAKE
    stepIndex = 0
    distanceM = stepStartM
    pushTrip()
    handler.removeCallbacks(tick)
    handler.postDelayed(tick, 1000)
  }

  private fun stopNavigation(notifyHost: Boolean) {
    if (mode == Mode.NONE) return
    mode = Mode.NONE
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

  private fun fakeStep(): Step {
    val s = steps[stepIndex]
    return Step.Builder(s.cue)
      .setManeuver(Maneuver.Builder(s.type).setIcon(maneuverIcon).build())
      .setRoad(s.road)
      .build()
  }

  private fun relayStep(g: NaviBridge.Guide): Step {
    val turn = TurnMap.of(g.turnType)
    val maneuver = Maneuver.Builder(turn.type).setIcon(maneuverIcon)
    if (turn.exitAngle > 0) maneuver.setRoundaboutExitAngle(turn.exitAngle)
    val cue = listOf(g.mainText, g.nearDirection, g.roadName).firstOrNull { it.isNotBlank() } ?: turn.label
    val step = Step.Builder(cue).setManeuver(maneuver.build())
    if (g.roadName.isNotBlank()) step.setRoad(g.roadName)
    return step.build()
  }

  private fun remainingTotalM(): Double = distanceM + (steps.size - 1 - stepIndex) * stepStartM

  private fun estimate(meters: Double, mps: Double, seconds: Long = (meters / mps).toLong()): TravelEstimate =
    TravelEstimate.Builder(
      Distance.create(meters, Distance.UNIT_METERS),
      DateTimeWithZone.create(ZonedDateTime.now().plusSeconds(seconds)),
    ).setRemainingTimeSeconds(seconds).build()

  private fun guideEstimate(g: NaviBridge.Guide): TravelEstimate =
    estimate(g.distanceM.toDouble(), 1.0, g.timeSec.toLong().coerceAtLeast(0))

  private fun relayDestination(s: NaviBridge.State): TravelEstimate? {
    val m = s.remainM ?: return null
    return estimate(m.toDouble(), 1.0, (s.remainSec ?: 0).toLong().coerceAtLeast(0))
  }

  private fun pushTrip() {
    val s = steps[stepIndex]
    val trip = Trip.Builder()
      .addStep(fakeStep(), estimate(distanceM, stepRateMps))
      .addDestination(Destination.Builder().setName("AA Probe 시험").setAddress("가짜 경로").build(),
        estimate(remainingTotalM(), stepRateMps))
      .setCurrentRoad("AA Probe 시험로")
      .build()
    sendTrip(trip, mapOf("mode" to "fake", "step" to stepIndex, "maneuver_type" to s.type, "cue" to s.cue, "distance_m" to distanceM))
  }

  private fun pushRelayTrip(s: NaviBridge.State, cur: NaviBridge.Guide) {
    val trip = Trip.Builder().addStep(relayStep(cur), guideEstimate(cur))
    s.next?.let { trip.addStep(relayStep(it), guideEstimate(it)) }
    relayDestination(s)?.let {
      trip.addDestination(Destination.Builder().setName("목적지").build(), it)
    }
    if (cur.roadName.isNotBlank()) trip.setCurrentRoad(cur.roadName)
    val turn = TurnMap.of(cur.turnType)
    sendTrip(trip.build(), mapOf(
      "mode" to "relay", "turn_type" to cur.turnType, "maneuver_type" to turn.type, "cue" to turn.label,
      "distance_m" to cur.distanceM, "next_turn_type" to s.next?.turnType, "remain_m" to s.remainM,
    ))
  }

  private fun sendTrip(trip: Trip, fields: Map<String, Any?>) {
    try {
      navigationManager.updateTrip(trip)
      lastTripError = null
      ProbeLog.log("tbt", fields)
    } catch (e: Exception) {
      lastTripError = "${e.javaClass.simpleName}: ${e.message}"
      ProbeLog.log("tbt", fields + mapOf("ok" to false, "error" to lastTripError))
    }
    invalidate()
    renderer.requestRender()
  }

  private fun overlayLines(): List<String> {
    val header = mutableListOf("AA Probe  api=${carContext.carAppApiLevel}")
    val b = NaviBridge.state
    header += "중계 ${if (relayArmed) "켜짐" else "꺼짐"}  수신 ${NaviBridgeService.lastStatus}  연결 ${b.connections}"
    header += when (mode) {
      Mode.FAKE -> {
        val s = steps[stepIndex]
        "TBT 시험  #$stepIndex ${s.cue} ${distanceM.toInt()}m  (type ${s.type})"
      }
      Mode.RELAY -> b.current?.let {
        "중계 중  ${TurnMap.of(it.turnType).label} ${it.distanceM}m  (code ${it.turnType})  ${it.roadName}"
      } ?: "중계 중  안내 없음"
      Mode.NONE -> if (relayArmed) "중계 대기: TMAP/네이버에서 안내를 시작하세요" else "TBT OFF  데이터 수집 중"
    }
    lastTripError?.let { header += "TBT 오류: $it" }
    return header + sensors.summaryLines()
  }
}
