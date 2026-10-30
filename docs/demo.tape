# ==============================================================================
# VHS Tape: Northside Barbers Conversational Booking Demonstration
#
# Prerequisites:
#   1. Application stack running via the README Quick Start:
#        docker compose up -d app db
#   2. Local .env file configured with WEBHOOK_SECRET (and LLM_PROVIDER=fake)
#
# Command to regenerate:
#   vhs docs/demo.tape
# ==============================================================================

Output docs/assets/demo.gif

Set Shell "powershell"
Set FontSize 13
Set Width 800
Set Height 580
Set Padding 16
Set Theme "Catppuccin Mocha"
Set Framerate 8
Set TypingSpeed 40ms

Hide
Type "function prompt { '$ ' }; Clear-Host"
Enter
Sleep 1s
Show

Type "uv run python scripts/demo_conversation.py --pace 1.5"
Sleep 500ms
Enter

# Wait for the 7 demo conversation turns to complete (~13s with pace 1.5)
Sleep 14s

# Pause on the final turn and completion banner before the GIF loops
Sleep 3s
