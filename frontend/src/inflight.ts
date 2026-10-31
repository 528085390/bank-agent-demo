/**
 * The reads that are already on their way.
 *
 * A pointer resting on a conversation row starts reading it; the click that follows a moment
 * later asks for the same two things. Sharing the promise means the second caller waits on the
 * read the first one started instead of paying for it twice -- and against a database a round
 * trip away, that read is the whole cost of a switch.
 *
 * Nothing is remembered here: an entry lives only while its read is in flight, so a later caller
 * asks the server again. That is what keeps a stale answer out of the cache.
 */
const pending = new Map<string, Promise<unknown>>()

/** Runs `read` unless an identical read is already on its way, in which case that one is reused. */
export function share<T>(key: string, read: () => Promise<T>): Promise<T> {
  const running = pending.get(key) as Promise<T> | undefined
  if (running) return running
  const started = Promise.resolve().then(read)
  pending.set(key, started)
  // `then(done, done)` on purpose: a read that fails must not leave an unhandled rejection
  // behind, and the entry goes either way.
  const done = () => { if (pending.get(key) === started) pending.delete(key) }
  started.then(done, done)
  return started
}
