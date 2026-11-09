// Radio Guard recovery sheet: a hidden Home Assistant page (no sidebar entry) that loads the sheet
// with the signed-in user's own credentials and shows it full-page, ready to print.
class RadioGuardRecovery extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._started) {
      this._started = true;
      this._load();
    }
  }

  async _load() {
    this.style.cssText = "display:flex;flex-direction:column;height:100vh;background:#fff";
    const bar = document.createElement("div");
    bar.style.cssText =
      "display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid #ddd;font:14px system-ui";
    const back = document.createElement("button");
    back.textContent = "← Back";
    back.onclick = () => history.back();
    const print = document.createElement("button");
    print.textContent = "Print";
    print.disabled = true;
    bar.append(back, print);
    const frame = document.createElement("iframe");
    frame.title = "Recovery sheet";
    frame.style.cssText = "flex:1;border:0;width:100%;background:#fff";
    this.replaceChildren(bar, frame);
    try {
      const resp = await this._hass.fetchWithAuth("/api/radio_guard/recovery_sheet");
      const html = await resp.text();
      if (!resp.ok) throw new Error(html || resp.statusText);
      frame.srcdoc = html;
      print.disabled = false;
      print.onclick = () => frame.contentWindow.print();
    } catch (err) {
      frame.srcdoc = `<p style="font:15px system-ui;padding:16px">Could not load the recovery sheet: ${String(
        err.message || err
      ).replace(/[<&]/g, "")}</p>`;
    }
  }
}
customElements.define("radio-guard-recovery", RadioGuardRecovery);
