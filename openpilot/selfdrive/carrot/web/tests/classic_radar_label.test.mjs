// classic 0x238 주행정보 라벨의 검증 필드와 상태 표시를 확인한다.
import assert from "node:assert/strict";
import test from "node:test";

import { formatClassicRadarLabel } from "../src/features/drive/contents/vision/road_overlay_lead_renderer.js";

test("formats confirmed classic radar information without decoding unknown frames", () => {
  assert.equal(formatClassicRadarLabel({
    trackId: 7,
    trackState: 2,
    dRel: 12.8,
    yRel: -1.8,
    vLead: 0.1,
    vRel: -10.2,
  }), "238 T7 S2C 13m y-1.8 V0.1 Δ-10.2m/s");
});

test("marks non-confirmed states as unresolved", () => {
  assert.equal(formatClassicRadarLabel({
    trackId: 3,
    trackState: 1,
    dRel: 9.4,
    yRel: 3.8,
    vLead: 0,
    vRel: 0,
  }), "238 T3 S1U 9m y+3.8 V0.0 Δ+0.0m/s");
});
