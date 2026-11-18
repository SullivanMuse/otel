import './telemetry'
import { traceIncomingEvents, tracedEmit } from './socket-tracing'
import { LitElement, css, html } from 'lit'
import { customElement, state } from 'lit/decorators.js'
import { io } from 'socket.io-client'

const sio = io();
sio.on("connect", () => {
  console.log("sio connected");
});
traceIncomingEvents(sio);

interface SupplyStatus {
  voltage: number
  current: number
  power: number
  voltage_setpoint: number
  current_limit: number
  ovp_level: number
  ovp_tripped: boolean
  output_enabled: boolean
  mode: string
}

@customElement('my-element')
export class MyElement extends LitElement {
  @state()
  private status?: SupplyStatus

  private onStatus = (status: SupplyStatus) => {
    this.status = status
  }

  connectedCallback() {
    super.connectedCallback()
    sio.on("status", this.onStatus)
  }

  disconnectedCallback() {
    sio.off("status", this.onStatus)
    super.disconnectedCallback()
  }

  @state()
  private commandError?: string

  @state()
  private busy = false

  private async send(event: string, value: number) {
    this.busy = true
    this.commandError = undefined
    try {
      const reply = await tracedEmit<{ ok: boolean; error?: string }>(sio, event, { value })
      if (!reply.ok) this.commandError = reply.error ?? `${event} failed`
    } catch (err) {
      this.commandError = `${event}: ${err instanceof Error ? err.message : err}`
    } finally {
      this.busy = false
    }
  }

  private onSetpointSubmit(event: SubmitEvent, name: 'setVoltage' | 'setCurrent') {
    event.preventDefault()
    const input = (event.target as HTMLFormElement).elements.namedItem('value') as HTMLInputElement
    const value = input.valueAsNumber
    if (!Number.isFinite(value)) {
      this.commandError = 'Enter a number'
      return
    }
    void this.send(name, value)
  }

  private toggleOutput() {
    if (this.status === undefined) return
    void this.send('setOutput', this.status.output_enabled ? 0 : 1)
  }

  render() {
    const s = this.status
    return html`
      <table>
        <tr>
          <th>Name</th>
          <th>Value</th>
        </tr>
        <tr>
          <td>Output Voltage (V)</td>
          <td>${fmt(s?.voltage, 3)}</td>
        </tr>
        <tr>
          <td>Output Current (A)</td>
          <td>${fmt(s?.current, 3)}</td>
        </tr>
        <tr>
          <td>Output Power (W)</td>
          <td>${fmt(s?.power, 3)}</td>
        </tr>
        <tr>
          <td>Voltage Limit (V)</td>
          <td>${fmt(s?.voltage_setpoint, 3)}</td>
        </tr>
        <tr>
          <td>Current Limit (A)</td>
          <td>${fmt(s?.current_limit, 3)}</td>
        </tr>
        <tr>
          <td>Mode</td>
          <td>${s?.mode ?? '—'}</td>
        </tr>
        <tr>
          <td>Output Enabled?</td>
          <td>${s === undefined ? '—' : s.output_enabled ? 'Yes' : 'No'}</td>
        </tr>
      </table>

      <div class="controls">
        <form @submit=${(e: SubmitEvent) => this.onSetpointSubmit(e, 'setVoltage')}>
          <label for="volt">Voltage limit (V)</label>
          <input id="volt" name="value" type="number" step="any" min="0" required
            placeholder=${fmt(s?.voltage_setpoint, 3)} ?disabled=${this.busy} />
          <button type="submit" ?disabled=${this.busy}>Set</button>
        </form>
        <form @submit=${(e: SubmitEvent) => this.onSetpointSubmit(e, 'setCurrent')}>
          <label for="curr">Current limit (A)</label>
          <input id="curr" name="value" type="number" step="any" min="0" required
            placeholder=${fmt(s?.current_limit, 3)} ?disabled=${this.busy} />
          <button type="submit" ?disabled=${this.busy}>Set</button>
        </form>
        <button
          class=${s?.output_enabled ? 'output on' : 'output'}
          ?disabled=${this.busy || s === undefined}
          @click=${this.toggleOutput}
        >
          ${s?.output_enabled ? 'Disable output' : 'Enable output'}
        </button>
        ${this.commandError ? html`<p class="error" role="alert">${this.commandError}</p>` : ''}
      </div>
    `
  }

  static styles = css`
    .controls {
      display: grid;
      gap: 0.5rem;
      margin-top: 1rem;
      max-width: 24rem;
    }
    form {
      display: grid;
      grid-template-columns: 9rem 1fr auto;
      gap: 0.5rem;
      align-items: center;
    }
    input {
      min-width: 0;
    }
    .output.on {
      background: #b33;
      color: white;
    }
    .error {
      color: #c33;
      margin: 0;
    }
  `
}

function fmt(value: number | undefined, digits: number): string {
  return value === undefined ? '—' : value.toFixed(digits)
}

declare global {
  interface HTMLElementTagNameMap {
    'my-element': MyElement
  }
}
