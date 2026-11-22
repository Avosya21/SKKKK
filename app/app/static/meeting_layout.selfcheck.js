// Самопроверка meeting_layout.js в браузере (console.assert), без тестового фреймворка.
(function () {
  const layout = window.PrimaryAnaliticsLayout;
  console.assert(layout.flagState(null, {}) === layout.FLAG_NONE, "publication=null → none");
  console.assert(layout.flagState({ isMuted: true }, {}) === layout.FLAG_OFF, "muted → off");
  console.assert(
    layout.flagState({ isMuted: false }, { mic_silent: "1" }) === layout.FLAG_SILENT,
    "mic_silent → silent",
  );
  console.assert(layout.flagState({ isMuted: false }, {}) === layout.FLAG_ON, "live → on");
  console.assert(
    layout.flagState(null, { send_audio: "1" }, "send_audio") === layout.FLAG_ON,
    "missing publication keeps send_audio",
  );
  console.assert(
    layout.flagState(null, { send_audio: "0" }, "send_audio") === layout.FLAG_OFF,
    "missing publication send_audio off",
  );
  console.assert(layout.audioSubscribe(false, false) === false, "sound off, no record → unsubscribe");
  console.assert(layout.audioSubscribe(false, true) === true, "record holds the mic subscription");
  console.assert(layout.audioSubscribe(true, false) === true, "sound on → subscribe");
  console.assert(layout.speakerHold("a", 9000, 10000, "b", 2000) === "a", "speaker holds for 2s");
  console.assert(layout.speakerHold("a", 7000, 10000, "b", 2000) === "b", "speaker yields after 2s");
  const pubs = [{ id: "a" }, { id: "b" }];
  console.assert(
    layout.soundPlan(false, pubs).every(function (row) {
      return row.subscribed === false;
    }),
    "sound off → unsubscribe all",
  );
  const order = layout.tileOrder(
    [{ id: "meeting-tile-a" }, { id: "meeting-tile-b" }],
    [
      { sid: "old", personId: "a" },
      { sid: "new", personId: "b" },
    ],
    { old: 1, new: 2 },
  );
  console.assert(order[0].lead === true && order[0].id === "meeting-tile-screen-new", "fresh screen is lead");
  console.assert(order[1].lead === false, "older screen is not lead");
  console.assert(layout.qualityBadge("poor") === "плохая связь", "poor badge");
  console.assert(layout.qualityBadge("excellent") === "", "good quality is empty");
})();
