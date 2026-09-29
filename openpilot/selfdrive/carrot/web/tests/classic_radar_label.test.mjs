// classic 0x238 주행정보 라벨은 레이더가 보고한 객체 ID만 표시한다.
import assert from "node:assert/strict";
import test from "node:test";

import { formatClassicRadarLabel } from "../src/features/drive/contents/vision/road_overlay_lead_renderer.js";

test("shows only the radar object ID", () => {
  assert.equal(formatClassicRadarLabel({
    trackId: 7,
    trackState: 2,
    dRel: 12.8,
    yRel: -1.8,
    vLead: 0.1,
    vRel: -10.2,
  }), "7");
  assert.equal(formatClassicRadarLabel({ trackId: 0, trackState: 1 }), "0");
  assert.equal(formatClassicRadarLabel({ trackId: 63, trackState: 2 }), "63");
});

test("hides fallback or missing IDs", () => {
  assert.equal(formatClassicRadarLabel({ trackId: 1003, trackState: 2 }), "-");
  assert.equal(formatClassicRadarLabel({}), "-");
});
