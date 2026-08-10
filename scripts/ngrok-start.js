const { spawn } = require('child_process')

const proc = spawn('ngrok', ['http', '3000', '--domain', 'parasail-ethically-finishing.ngrok-free.dev'], {
  stdio: 'inherit',
  shell: true
})

proc.on('exit', (code) => process.exit(code))
