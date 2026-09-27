// 주행 정보 전방 그래프의 레이더 객체 라벨을 검증한다.
import assert from "node:assert/strict";
import test from "node:test";

import {
  createDriveInsightsForwardScene,
  entityLabel,
} from "../src/features/drive/contents/drive_insights/forward.js";

test("forward radar points show distance and relative speed without selection", () => {
  const scene = createDriveInsightsForwardScene({
    radar: [
      { id: "classic238:2", source: "front", xM: 31.4, yM: 1.2, relativeSpeedMps: -2.5 },
      { id: "classic238:3", source: "front", xM: 45, yM: -1.2 },
    ],
  }, { width: 280, height: 320 });

  assert.equal(scene.entities.length, 2);
  assert.equal(scene.entities[0].selected, false);
  assert.equal(scene.entities[0].presentation.showLabel, true);
  assert.equal(entityLabel(scene.entities[0]), "31m Δ-2.5m/s");
  assert.equal(entityLabel(scene.entities[1]), "45m");
});

test("forward presentation can still hide or replace a radar label", () => {
  const scene = createDriveInsightsForwardScene({
    radar: [{ id: "r1", source: "front", xM: 20, yM: 0, relativeSpeedMps: 1 }],
  }, { width: 280, height: 320 }, { r1: { showLabel: false, label: "custom" } });

  assert.equal(scene.entities[0].presentation.showLabel, false);
  assert.equal(entityLabel(scene.entities[0]), "custom");
});
