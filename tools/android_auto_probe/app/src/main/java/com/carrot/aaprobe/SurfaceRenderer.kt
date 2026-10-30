package com.carrot.aaprobe

import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.view.Surface
import androidx.car.app.AppManager
import androidx.car.app.CarContext
import androidx.car.app.SurfaceCallback
import androidx.car.app.SurfaceContainer

/** Draws the probe status as plain text on the navigation map surface (no map tiles). */
class SurfaceRenderer(carContext: CarContext, private val lines: () -> List<String>) : SurfaceCallback {
  private val handler = Handler(Looper.getMainLooper())
  private val paint = Paint().apply {
    color = Color.WHITE
    isAntiAlias = true
  }
  private var surface: Surface? = null
  private var width = 0
  private var height = 0
  private var visibleArea: Rect? = null
  private var renderPending = false

  init {
    carContext.getCarService(AppManager::class.java).setSurfaceCallback(this)
  }

  override fun onSurfaceAvailable(container: SurfaceContainer) {
    surface = container.surface
    width = container.width
    height = container.height
    ProbeLog.log("surface", mapOf("w" to width, "h" to height, "dpi" to container.dpi))
    render()
  }

  override fun onVisibleAreaChanged(visibleArea: Rect) {
    this.visibleArea = visibleArea
    render()
  }

  override fun onStableAreaChanged(stableArea: Rect) = Unit

  override fun onSurfaceDestroyed(container: SurfaceContainer) {
    surface = null
  }

  /** Coalesces frequent sensor updates into at most 5 redraws per second. */
  fun requestRender() {
    if (renderPending) return
    renderPending = true
    handler.postDelayed({
      renderPending = false
      render()
    }, 200)
  }

  fun render() {
    val s = surface ?: return
    if (!s.isValid) return
    val canvas = try {
      s.lockCanvas(null)
    } catch (e: Exception) {
      return
    } ?: return
    try {
      canvas.drawColor(Color.rgb(16, 24, 32))
      val area = visibleArea?.takeIf { !it.isEmpty } ?: Rect(0, 0, width, height)
      val textSize = (area.height() / 18f).coerceIn(14f, 32f)
      paint.textSize = textSize
      var y = area.top + textSize * 1.4f
      for (line in lines()) {
        if (y > area.bottom) break
        canvas.drawText(line, area.left + textSize * 0.6f, y, paint)
        y += textSize * 1.3f
      }
    } finally {
      s.unlockCanvasAndPost(canvas)
    }
  }
}
