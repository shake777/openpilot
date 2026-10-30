package com.carrot.aaprobe

import android.os.SystemClock
import androidx.car.app.CarContext
import androidx.car.app.hardware.CarHardwareManager
import androidx.car.app.hardware.common.CarValue
import androidx.car.app.hardware.common.OnCarDataAvailableListener
import androidx.car.app.hardware.info.Accelerometer
import androidx.car.app.hardware.info.CarHardwareLocation
import androidx.car.app.hardware.info.CarInfo
import androidx.car.app.hardware.info.CarSensors
import androidx.car.app.hardware.info.Compass
import androidx.car.app.hardware.info.EnergyLevel
import androidx.car.app.hardware.info.EnergyProfile
import androidx.car.app.hardware.info.Gyroscope
import androidx.car.app.hardware.info.Mileage
import androidx.car.app.hardware.info.Model
import androidx.car.app.hardware.info.Speed
import androidx.car.app.hardware.info.TollCard
import androidx.car.app.versioning.CarAppApiLevels
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Subscribes to every CarInfo/CarSensors value the Car App Library exposes and records
 * what the head unit actually provides (status per field, values, update counts).
 */
class SensorProbe(private val carContext: CarContext, private val onChange: () -> Unit) {
  private val executor = ContextCompat.getMainExecutor(carContext)
  private val latest = LinkedHashMap<String, String>()
  private val counts = HashMap<String, Int>()
  private val lastLogged = HashMap<String, Long>()

  private var info: CarInfo? = null
  private var sensors: CarSensors? = null

  private val speedListener = OnCarDataAvailableListener<Speed> {
    record("speed", mapOf(
      "raw_mps" to cv(it.rawSpeedMetersPerSecond),
      "display_mps" to cv(it.displaySpeedMetersPerSecond),
      "unit" to cv(it.speedDisplayUnit),
    ))
  }
  private val mileageListener = OnCarDataAvailableListener<Mileage> {
    record("mileage", mapOf("odometer_m" to cv(it.odometerMeters), "unit" to cv(it.distanceDisplayUnit)))
  }
  private val energyLevelListener = OnCarDataAvailableListener<EnergyLevel> {
    record("energy_level", mapOf(
      "fuel_pct" to cv(it.fuelPercent),
      "battery_pct" to cv(it.batteryPercent),
      "energy_low" to cv(it.energyIsLow),
      "range_m" to cv(it.rangeRemainingMeters),
      "unit" to cv(it.distanceDisplayUnit),
    ))
  }
  private val tollListener = OnCarDataAvailableListener<TollCard> {
    record("toll", mapOf("card_state" to cv(it.cardState)))
  }
  private val accelListener = OnCarDataAvailableListener<Accelerometer> {
    record("accelerometer", mapOf("forces" to cv(it.forces)))
  }
  private val gyroListener = OnCarDataAvailableListener<Gyroscope> {
    record("gyroscope", mapOf("rotations" to cv(it.rotations)))
  }
  private val compassListener = OnCarDataAvailableListener<Compass> {
    record("compass", mapOf("orientations" to cv(it.orientations)))
  }
  private val locationListener = OnCarDataAvailableListener<CarHardwareLocation> {
    record("car_location", mapOf("location" to cv(it.location)))
  }

  fun start() {
    val api = carContext.carAppApiLevel
    ProbeLog.log("host", mapOf(
      "car_app_api_level" to api,
      "host_package" to carContext.hostInfo?.packageName,
      "host_uid" to carContext.hostInfo?.uid,
    ))
    if (api < CarAppApiLevels.LEVEL_3) {
      note("hardware", "UNSUPPORTED host API $api < 3")
      return
    }
    val hw = try {
      carContext.getCarService(CarHardwareManager::class.java)
    } catch (e: Exception) {
      fail("hardware", e)
      return
    }
    val carInfo = hw.carInfo.also { info = it }
    val carSensors = hw.carSensors.also { sensors = it }

    attempt("model") {
      carInfo.fetchModel(executor) { m: Model ->
        record("model", mapOf("name" to cv(m.name), "year" to cv(m.year), "manufacturer" to cv(m.manufacturer)), force = true)
      }
    }
    attempt("energy_profile") {
      carInfo.fetchEnergyProfile(executor) { p: EnergyProfile ->
        record("energy_profile", mapOf("fuel_types" to cv(p.fuelTypes), "ev_connectors" to cv(p.evConnectorTypes)), force = true)
      }
    }
    attempt("speed") { carInfo.addSpeedListener(executor, speedListener) }
    attempt("mileage") { carInfo.addMileageListener(executor, mileageListener) }
    attempt("energy_level") { carInfo.addEnergyLevelListener(executor, energyLevelListener) }
    attempt("toll") { carInfo.addTollListener(executor, tollListener) }

    val rate = CarSensors.UPDATE_RATE_NORMAL
    attempt("accelerometer") { carSensors.addAccelerometerListener(rate, executor, accelListener) }
    attempt("gyroscope") { carSensors.addGyroscopeListener(rate, executor, gyroListener) }
    attempt("compass") { carSensors.addCompassListener(rate, executor, compassListener) }
    attempt("car_location") { carSensors.addCarHardwareLocationListener(rate, executor, locationListener) }
  }

  fun stop() {
    info?.let { i ->
      attempt("remove_speed", quiet = true) { i.removeSpeedListener(speedListener) }
      attempt("remove_mileage", quiet = true) { i.removeMileageListener(mileageListener) }
      attempt("remove_energy_level", quiet = true) { i.removeEnergyLevelListener(energyLevelListener) }
      attempt("remove_toll", quiet = true) { i.removeTollListener(tollListener) }
    }
    sensors?.let { s ->
      attempt("remove_accelerometer", quiet = true) { s.removeAccelerometerListener(accelListener) }
      attempt("remove_gyroscope", quiet = true) { s.removeGyroscopeListener(gyroListener) }
      attempt("remove_compass", quiet = true) { s.removeCompassListener(compassListener) }
      attempt("remove_car_location", quiet = true) { s.removeCarHardwareLocationListener(locationListener) }
    }
    info = null
    sensors = null
  }

  fun restart() {
    stop()
    synchronized(latest) {
      latest.clear()
      counts.clear()
    }
    ProbeLog.log("sensor_restart")
    start()
    onChange()
  }

  fun summaryLines(): List<String> = synchronized(latest) {
    latest.map { (k, v) -> "$k(${counts[k] ?: 0}) $v" }
  }

  private fun attempt(name: String, quiet: Boolean = false, block: () -> Unit) {
    try {
      block()
      if (!quiet) ProbeLog.log("subscribe", mapOf("name" to name, "ok" to true))
    } catch (e: Exception) {
      if (!quiet) fail(name, e)
    }
  }

  private fun fail(name: String, e: Exception) {
    ProbeLog.log("subscribe", mapOf("name" to name, "ok" to false, "error" to "${e.javaClass.simpleName}: ${e.message}"))
    note(name, "ERROR ${e.javaClass.simpleName}")
  }

  private fun note(name: String, text: String) {
    synchronized(latest) { latest[name] = text }
    onChange()
  }

  /** Every update updates the on-screen summary; the log file gets at most 5 lines/s per item. */
  private fun record(name: String, fields: Map<String, JSONObject>, force: Boolean = false) {
    val count: Int
    synchronized(latest) {
      count = (counts[name] ?: 0) + 1
      counts[name] = count
      latest[name] = fields.entries.joinToString(" ") { (k, v) -> "$k=${shortText(v)}" }
    }
    val now = SystemClock.elapsedRealtime()
    val last = lastLogged[name] ?: 0L
    if (force || count == 1 || now - last >= 200) {
      lastLogged[name] = now
      ProbeLog.log("data", mapOf("name" to name, "count" to count) + fields)
    }
    onChange()
  }

  private fun cv(value: CarValue<*>): JSONObject = JSONObject().apply {
    put("status", statusName(value.status))
    put("value", ProbeLog.toJson(value.value))
    put("ts", value.timestampMillis)
  }

  private fun shortText(v: JSONObject): String {
    val status = v.optString("status")
    if (status != "SUCCESS") return status
    val value = v.opt("value")
    return when (value) {
      is JSONObject -> if (value.has("lat")) {
        "%.6f,%.6f".format(value.optDouble("lat"), value.optDouble("lon"))
      } else value.toString()
      is Double -> "%.3f".format(value)
      else -> value?.toString()?.take(48) ?: "null"
    }
  }

  private fun statusName(status: Int): String = when (status) {
    CarValue.STATUS_SUCCESS -> "SUCCESS"
    CarValue.STATUS_UNIMPLEMENTED -> "UNIMPLEMENTED"
    CarValue.STATUS_UNAVAILABLE -> "UNAVAILABLE"
    CarValue.STATUS_UNKNOWN -> "UNKNOWN"
    else -> "STATUS_$status"
  }
}
