/**
 * Headless render smoke test: mounts the real components against the real API
 * (server must be on :8787) inside jsdom and fails on any React error or
 * unhandled rejection. Catches crashes that `vite build` cannot.
 *
 * Run:  node smoke.test.mjs
 */
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost:8787/',
  pretendToBeVisual: true,
})
global.window = dom.window
global.document = dom.window.document
Object.defineProperty(global, 'navigator', {
  value: dom.window.navigator, configurable: true, writable: true,
})
global.HTMLElement = dom.window.HTMLElement
global.Node = dom.window.Node
global.Element = dom.window.Element
global.getComputedStyle = dom.window.getComputedStyle
global.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
global.cancelAnimationFrame = clearTimeout
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
global.IS_REACT_ACT_ENVIRONMENT = true
global.localStorage = dom.window.localStorage

// Route the app's relative /api calls at the local server.
const realFetch = global.fetch
global.fetch = (url, opts) =>
  realFetch(typeof url === 'string' && url.startsWith('/') ? 'http://localhost:8787' + url : url, opts)

const errors = []
const origError = console.error
console.error = (...a) => { errors.push(a.join(' ')); origError(...a) }
process.on('unhandledRejection', (e) => errors.push('unhandledRejection: ' + e))

const React = (await import('react')).default
const { createRoot } = await import('react-dom/client')
const { act } = await import('react')

// Vite transforms the JSX for us so this stays the same code the browser runs.
const { createServer } = await import('vite')
const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
const App = (await vite.ssrLoadModule('/src/App.jsx')).default
const Review = (await vite.ssrLoadModule('/src/Review.jsx')).default
const Repertoire = (await vite.ssrLoadModule('/src/Repertoire.jsx')).default
const Play = (await vite.ssrLoadModule('/src/Play.jsx')).default

const settle = async (ms = 900) => {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)) })
}

const root = createRoot(document.getElementById('root'))

// 1. Library view
await act(async () => { root.render(React.createElement(App)) })
await settle()
const html = document.getElementById('root').innerHTML
console.log('library rendered:', html.includes('chess trainer'))
console.log('rows present:', (html.match(/<tr/g) || []).length - 1)

// 2. Review view on a real analyzed game
const list = await (await global.fetch('/api/games?analyzed=true&limit=1')).json()
const gameId = list.games[0].id
console.log('review game:', gameId)

await act(async () => {
  root.render(React.createElement(Review, { gameId, onBack: () => {} }))
})
await settle(1500)
const rhtml = document.getElementById('root').innerHTML
console.log('board mounted:', rhtml.includes('cg-wrap'))
console.log('movelist present:', rhtml.includes('moves'))
console.log('coach/notes panels:', rhtml.includes('notes'))

// 3. Step forward through a few moves via the keyboard handler
for (let i = 0; i < 5; i++) {
  await act(async () => {
    document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowRight' }))
    window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowRight' }))
  })
}
await settle(400)
console.log('stepped without crash: true')

// 3b. play vs engine: from a position where the OPPONENT is to move, enabling
// the toggle must make the engine move for them (and never for me).
{
  const g = await (await global.fetch(`/api/games/${gameId}`)).json()
  const myColor = g.game.my_color
  const firstOppPly = g.positions.findIndex((p) => p.side !== myColor)
  await act(async () => { root.render(React.createElement(Review, { gameId, onBack: () => {} })) })
  await settle(1200)
  for (let i = 0; i <= firstOppPly; i++) {
    await act(async () => {
      window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowRight' }))
    })
  }
  await settle(300)
  const boxes = [...document.querySelectorAll('input[type=checkbox]')]
  const toggle = boxes[1]   // [0] = coach mode, [1] = play vs engine
  console.log('opponent-to-move ply:', firstOppPly, '| toggle found:', !!toggle)
  await act(async () => { toggle.click() })
  await settle(4000)
  const h = document.getElementById('root').innerHTML
  console.log('engine moved for opponent (branch opened):', h.includes('exploring'))
  console.log('engine label present:', h.includes('engine replies'))
}

// 4. Repertoire view (board + engine recommendations)
await act(async () => { root.render(React.createElement(Repertoire)) })
await settle(16000)
const phtml = document.getElementById('root').innerHTML
console.log('repertoire board:', phtml.includes('cg-wrap'))
console.log('repertoire leaks listed:', (phtml.match(/rep-item/g) || []).length)
console.log('recommendations shown:', phtml.includes('play instead'))

// 5. Free-play tab: engine must answer as the OTHER side only.
{
  await act(async () => { root.render(React.createElement(Play)) })
  await settle(3000)
  let h = document.getElementById('root').innerHTML
  console.log('play board:', h.includes('cg-wrap'))
  console.log('eval shown:', /eval\s-?[0-9M]/.test(h.replace(/<[^>]+>/g, ' ')))

  // Play 1.e4 by invoking chessground's own move handler via a DOM drag is not
  // possible headlessly, so drive the FEN input instead: load a position where
  // it is black to move and confirm the engine (playing black) answers.
  const inputs = [...document.querySelectorAll('input[type=text], input:not([type])')]
  const fenInput = inputs[inputs.length - 1]
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
  await act(async () => {
    setter.call(fenInput, 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1')
    fenInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  })
  const loadBtn = [...document.querySelectorAll('button')].find((b) => b.textContent === 'load')
  await act(async () => { loadBtn.click() })
  await settle(4000)
  h = document.getElementById('root').innerHTML
  const moveEls = [...document.querySelectorAll('.moves .move')].map((e) => e.textContent.trim())
  console.log('engine answered as black:', moveEls.length > 0, JSON.stringify(moveEls))
  console.log('engine played exactly one move (did not take white too):', moveEls.length === 1)
}

// 6. Ask the coach from the play tab (hits the local model end to end).
{
  const boxes = [...document.querySelectorAll('button')]
  const suggested = boxes.find((b) => b.textContent === "What's my plan here?")
  console.log('suggested question buttons:', !!suggested)
  if (suggested) {
    await act(async () => { suggested.click() })
    // local model cold start can take a while
    for (let i = 0; i < 30 && !document.querySelector('.qa-a'); i++) await settle(2000)
    const a = document.querySelector('.qa-a')
    console.log('coach answered:', !!a && a.textContent.length > 40)
    console.log('answer:', a ? a.textContent.slice(0, 120) : '(none)')
  }
}

const real = errors.filter((e) => !/not wrapped in act|ReactDOM.render|useLayoutEffect does nothing/i.test(e))
console.log('\nreact errors:', real.length)
real.slice(0, 6).forEach((e) => console.log('  -', e.slice(0, 300)))
process.exit(real.length ? 1 : 0)
