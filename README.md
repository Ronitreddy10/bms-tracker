# Tambola (Housie) Game

A modern, interactive Tambola game built with HTML, CSS, and JavaScript featuring ticket generation, number picking, and speech synthesis.

## Features

### 🎫 Ticket Generator
- Generate valid 9x3 grid tickets with numbers from 1 to 90
- Each row has exactly 5 numbers and 4 blanks
- Column rules strictly followed:
  - Column 1: 1–10
  - Column 2: 11–20
  - Column 3: 21–30
  - Column 4: 31–40
  - Column 5: 41–50
  - Column 6: 51–60
  - Column 7: 61–70
  - Column 8: 71–80
  - Column 9: 81–90
- Generate multiple tickets at once (1, 6, 12, or 24 tickets)

### 🎲 Random Number Picker
- Randomly pick numbers from 1 to 90 without repetition
- Clear visual display of current number with animations
- Maintains history of called numbers
- Visual feedback when numbers are called

### 🎨 Animated & Interactive UI
- Modern gradient design with glassmorphism effects
- Smooth animations for ticket reveal and number pop-up
- Win animations when rows are completed
- Responsive design for all screen sizes
- Interactive number grid showing called/available numbers

### 🔊 Speech Synthesis
- **Announcing picked numbers**: When a number is picked, the app speaks it out loud
- Uses Web Speech API for natural voice output
- Toggle sound on/off functionality
- Converts numbers to words (e.g., "fifty-five")

## How to Play

1. **Generate Tickets**: Click "Generate Tickets" to create new tickets
2. **Pick Numbers**: Click "Pick Number" to randomly select numbers
3. **Track Progress**: Watch the called numbers history and number grid
4. **Win Detection**: The app automatically detects when rows are completed
5. **Reset**: Use "Reset" to start over with all numbers available

## Game Rules

- Each ticket has 3 rows and 9 columns
- Each row must have exactly 5 numbers and 4 blanks
- Numbers in each column follow the specified ranges
- Win by completing rows (5 numbers in a row)
- Full house occurs when all 90 numbers are called

## Technical Features

- **Pure JavaScript**: No external dependencies
- **Web Speech API**: Built-in browser speech synthesis
- **CSS Grid & Flexbox**: Modern layout techniques
- **CSS Animations**: Smooth transitions and effects
- **Responsive Design**: Works on desktop, tablet, and mobile
- **Progressive Web App**: Can be installed on devices

## Browser Compatibility

- Chrome/Edge (recommended for best speech synthesis)
- Firefox
- Safari
- Mobile browsers

## Getting Started

1. Open `index.html` in a modern web browser
2. Allow microphone permissions for speech synthesis (if prompted)
3. Start generating tickets and picking numbers!

## File Structure

```
tambola-game/
├── index.html          # Main HTML structure
├── styles.css          # CSS styles and animations
├── script.js           # JavaScript game logic
└── README.md          # This file
```

## Features in Detail

### Ticket Generation Algorithm
- Ensures valid Tambola ticket structure
- Maintains column range constraints
- Guarantees exactly 5 numbers per row
- Prevents duplicate numbers within tickets

### Number Picking System
- True random selection without repetition
- Visual feedback with animations
- Automatic win detection
- Speech synthesis for accessibility

### UI/UX Highlights
- Glassmorphism design with backdrop blur
- Gradient backgrounds and buttons
- Smooth hover effects and transitions
- Mobile-responsive layout
- Accessibility features with speech

## Future Enhancements

- Save/load game states
- Multiple game modes
- Sound effects for interactions
- Export tickets to PDF
- Multiplayer support
- Custom voice selection

---

**Enjoy playing Tambola!** 🎉 # bms-tracker
