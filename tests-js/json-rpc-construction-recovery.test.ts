import { describe, expect, it } from 'vitest'

import { JsonRpcGatewayClient } from '../apps/shared/src/json-rpc-gateway'

class Socket extends EventTarget {
  readyState = 0
  close(): void {
    this.readyState = 3
    this.dispatchEvent(new Event('close'))
  }
  open(): void {
    this.readyState = 1
    this.dispatchEvent(new Event('open'))
  }
}

describe('gateway socket construction recovery', () => {
  it.each([false, true])('allows a retry after construction throws (close first: %s)', async closeFirst => {
    const failure = new SyntaxError('socket construction failed')
    const socket = new Socket()
    let attempts = 0

    const client = new JsonRpcGatewayClient({
      socketFactory: () => {
        if (++attempts === 1) {
          throw failure
        }

        return socket as unknown as WebSocket
      },
      heartbeatIntervalMs: 0,
      heartbeatDeadlineMs: 0
    })

    await expect(client.connect('ws://localhost')).rejects.toBe(failure)
    expect(client.connectionState).toBe('error')

    if (closeFirst) {
      client.close()
    }

    const retry = client.connect('ws://localhost')
    socket.open()
    await retry
    expect(attempts).toBe(2)
    expect(client.connectionState).toBe('open')
    client.close()
    expect(client.connectionState).toBe('closed')
  })
})
