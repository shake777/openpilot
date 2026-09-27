import assert from "node:assert/strict";
import test from "node:test";

import {
  createDriveInsightsLiveSource,
  normalizeDriveInsightsLiveSnapshot,
} from "../src/features/drive/contents/drive_insights/source_live.js";

function providerSnapshot(timestampMs, speedMps) {
  return {
    timestampMs,
    hudState: { carState: { vEgo: speedMps } },
    overlayState: {},
    receivedAtMonotonic: { carState: timestampMs },
    connectionState: "connected",
    routeId: null,
  };
}

test("Drive Insights live source exposes a cheap dirty signal and samples latest state lazily", () => {
  let current = providerSnapshot(10, 1);
  const providerListeners = new Set();
  const provider = {
    snapshot: () => current,
    subscribe(listener) {
      providerListeners.add(listener);
      return () => providerListeners.delete(listener);
    },
  };
  const source = createDriveInsightsLiveSource({ provider });
  let updates = 0;
  source.subscribeUpdates(() => { updates += 1; });

  current = providerSnapshot(20, 2);
  for (const listener of providerListeners) listener(current);
  current = providerSnapshot(30, 3);
  for (const listener of providerListeners) listener(current);

  assert.equal(updates, 2);
  assert.equal(source.snapshot().timestampMs, 30);
  assert.equal(source.snapshot().ego.speedMps, 3);
});

test("Drive Insights exposes classic 0x238 candidates without selecting them for control", () => {
  const snapshot = normalizeDriveInsightsLiveSnapshot({
    timestampMs: 1_000,
    hudState: { carState: { vEgo: 12 } },
    overlayState: {
      classicRadarTracks: {
        points: [{ trackId: 4, dRel: 31.5, yRel: -1.2, vRel: -2.5, measured: true }],
      },
    },
    receivedAtMonotonic: { carState: 990, classicRadarTracks: 980 },
    connectionState: "connected",
    routeId: null,
  });

  assert.equal(snapshot.radar.length, 1);
  assert.deepEqual(snapshot.radar[0], {
    id: "classic238:4",
    source: "front",
    xM: 31.5,
    yM: -1.2,
    relativeSpeedMps: -2.5,
    measured: true,
    selected: false,
  });
  assert.equal(snapshot.freshness.radar.state, "fresh");
});
