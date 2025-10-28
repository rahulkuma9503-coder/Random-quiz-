import os
import json
import random
import asyncio
import csv
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
PORT = int(os.getenv('PORT', 10000))

# File paths
QUIZZES_FILE = 'quizzes.json'
GROUPS_FILE = 'groups.json'
STATS_FILE = 'bot_stats.json'

# Global bot instance
bot_instance = None

class QuizBot:
    def __init__(self):
        self.application = None
        self.quizzes = self.load_data(QUIZZES_FILE, [])
        self.groups = self.load_data(GROUPS_FILE, [])
        self.stats = self.load_data(STATS_FILE, {
            'total_quizzes_sent': 0,
            'total_groups_reached': 0,
            'quizzes_added': 0,
            'bot_start_time': datetime.now().isoformat(),
            'last_quiz_sent': None,
            'group_engagement': {}
        })
        self.broadcast_mode = {}
        self.scheduler_task = None
        
    def load_data(self, filename, default):
        """Load data from JSON file"""
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default
    
    def save_data(self, filename, data):
        """Save data to JSON file"""
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type
        
        if chat_type == 'private':
            if user_id == ADMIN_USER_ID:
                keyboard = [
                    [InlineKeyboardButton("📊 View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")],
                    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
                    [InlineKeyboardButton("👥 Manage Groups", callback_data="manage_groups")],
                    [InlineKeyboardButton("📋 Export Data", callback_data="export_data")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    "👋 **Admin Dashboard**\n\n"
                    "I'm your Quiz Bot! Choose an option below:\n\n"
                    "📊 **Statistics** - View detailed bot analytics\n"
                    "📝 **Add Quiz** - Create and send me a poll to save as quiz\n"
                    "📢 **Broadcast** - Send message to all groups\n"
                    "👥 **Manage Groups** - View and manage groups\n"
                    "📋 **Export Data** - Export quizzes and stats\n\n"
                    "To add a quiz: Create a poll and send it to me!",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "👋 Hello! I'm a quiz bot that sends random poll quizzes every hour.\n\n"
                    "Add me to your group and make me an admin to start receiving fun quiz polls!"
                )
        else:
            # Bot added to a group
            await self.add_to_group(update)
    
    async def add_to_group(self, update: Update):
        """Handle bot being added to a group"""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title
        
        group_info = {
            'chat_id': chat_id,
            'title': chat_title,
            'added_date': datetime.now().isoformat(),
            'member_count': update.effective_chat.get_member_count() if update.effective_chat.get_member_count else 0,
            'quizzes_received': 0,
            'last_activity': datetime.now().isoformat()
        }
        
        # Check if group already exists
        existing_group = next((g for g in self.groups if g['chat_id'] == chat_id), None)
        
        if existing_group:
            existing_group.update(group_info)
            message = f"🎉 I'm back in {chat_title}! I'll continue sending quiz polls every hour."
        else:
            self.groups.append(group_info)
            message = f"🎉 Thanks for adding me to {chat_title}!\n\nI'll send random quiz polls every hour automatically!"
        
        self.save_data(GROUPS_FILE, self.groups)
        
        # Send welcome message with group controls for admin
        if update.effective_user.id == ADMIN_USER_ID:
            keyboard = [
                [InlineKeyboardButton("🚫 Remove from Group", callback_data=f"remove_group_{chat_id}")],
                [InlineKeyboardButton("📊 Group Stats", callback_data=f"group_stats_{chat_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message)
    
    async def handle_private_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle private messages from admin"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("I only accept commands from the admin.")
            return
        
        # Check if user is in broadcast mode
        if self.broadcast_mode.get(user_id):
            await self.send_broadcast(update, context, update.message.text)
            return
        
        # Check if it's a poll
        if update.message.poll:
            await self.save_poll_quiz(update, update.message.poll)
        else:
            await update.message.reply_text(
                "❌ Please send a poll to save as a quiz!\n\n"
                "To create a poll:\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. Send it to me\n\n"
                "I'll automatically save it as a quiz!"
            )
    
    async def save_poll_quiz(self, update: Update, poll):
        """Save a poll as a quiz"""
        quiz = {
            'id': update.message.message_id,
            'type': 'poll',
            'question': poll.question,
            'options': [option.text for option in poll.options],
            'is_anonymous': poll.is_anonymous,
            'allows_multiple_answers': poll.allows_multiple_answers,
            'correct_option_id': poll.correct_option_id if hasattr(poll, 'correct_option_id') else None,
            'added_date': datetime.now().isoformat(),
            'sent_count': 0,
            'last_sent': None,
            'engagement': 0
        }
        
        self.quizzes.append(quiz)
        self.stats['quizzes_added'] += 1
        self.save_data(QUIZZES_FILE, self.quizzes)
        self.save_data(STATS_FILE, self.stats)
        
        # Format options for display
        options_text = "\n".join([f"• {option}" for option in quiz['options']])
        
        await update.message.reply_text(
            f"✅ **Poll Quiz Saved Successfully!**\n\n"
            f"📝 **Question:** {quiz['question']}\n\n"
            f"📋 **Options:**\n{options_text}\n\n"
            f"📊 Total quizzes: {len(self.quizzes)}\n"
            f"👥 Will be sent to: {len(self.groups)} groups"
        )
    
    async def send_random_quiz(self):
        """Send a random quiz poll to all groups"""
        if not self.quizzes or not self.groups:
            return
        
        quiz = random.choice(self.quizzes)
        
        # Update quiz stats
        quiz['sent_count'] += 1
        quiz['last_sent'] = datetime.now().isoformat()
        
        # Update global stats
        self.stats['total_quizzes_sent'] += len(self.groups)
        self.stats['last_quiz_sent'] = datetime.now().isoformat()
        
        sent_to = 0
        for group in self.groups:
            try:
                if quiz['type'] == 'poll':
                    # Send as poll
                    message = await self.application.bot.send_poll(
                        chat_id=group['chat_id'],
                        question=f"🎯 Quiz Time: {quiz['question']}",
                        options=quiz['options'],
                        is_anonymous=quiz.get('is_anonymous', False),
                        allows_multiple_answers=quiz.get('allows_multiple_answers', False),
                        type=Poll.QUIZ if quiz.get('correct_option_id') is not None else Poll.REGULAR,
                        correct_option_id=quiz.get('correct_option_id'),
                        explanation="Check back later for results!",
                        open_period=0,  # No time limit
                    )
                
                # Update group stats
                group['quizzes_received'] += 1
                group['last_activity'] = datetime.now().isoformat()
                
                # Track engagement
                if str(group['chat_id']) not in self.stats['group_engagement']:
                    self.stats['group_engagement'][str(group['chat_id'])] = 0
                self.stats['group_engagement'][str(group['chat_id'])] += 1
                
                sent_to += 1
                await asyncio.sleep(0.5)  # Rate limiting
                
            except Exception as e:
                print(f"Failed to send to group {group['chat_id']}: {e}")
        
        self.save_data(QUIZZES_FILE, self.quizzes)
        self.save_data(STATS_FILE, self.stats)
        self.save_data(GROUPS_FILE, self.groups)
        
        print(f"📤 Sent quiz poll to {sent_to}/{len(self.groups)} groups at {datetime.now()}")
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed bot statistics"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        total_quizzes = len(self.quizzes)
        total_groups = len(self.groups)
        total_quizzes_sent = self.stats['total_quizzes_sent']
        quizzes_added = self.stats['quizzes_added']
        
        # Calculate active groups (active in last 7 days)
        week_ago = datetime.now() - timedelta(days=7)
        active_groups = len([
            g for g in self.groups 
            if datetime.fromisoformat(g['last_activity']) > week_ago
        ])
        
        # Most popular quiz
        most_sent = max(self.quizzes, key=lambda x: x.get('sent_count', 0)) if self.quizzes else None
        
        # Count poll types
        quiz_polls = len([q for q in self.quizzes if q.get('correct_option_id') is not None])
        regular_polls = len([q for q in self.quizzes if q.get('correct_option_id') is None])
        
        stats_text = (
            f"📊 **Detailed Bot Statistics**\n\n"
            f"📝 **Quizzes Database**\n"
            f"   • Total quizzes: {total_quizzes}\n"
            f"   • Quiz polls: {quiz_polls}\n"
            f"   • Regular polls: {regular_polls}\n"
            f"   • Quizzes added: {quizzes_added}\n"
            f"   • Most sent quiz: {most_sent['sent_count'] if most_sent else 0} times\n\n"
            
            f"👥 **Groups Analytics**\n"
            f"   • Total groups: {total_groups}\n"
            f"   • Active groups: {active_groups}\n"
            f"   • Total quizzes sent: {total_quizzes_sent}\n\n"
            
            f"⏰ **Performance**\n"
            f"   • Bot started: {datetime.fromisoformat(self.stats['bot_start_time']).strftime('%Y-%m-%d %H:%M')}\n"
            f"   • Last quiz sent: {datetime.fromisoformat(self.stats['last_quiz_sent']).strftime('%Y-%m-%d %H:%M') if self.stats['last_quiz_sent'] else 'Never'}\n"
            f"   • Next quiz in: ~1 hour\n\n"
            
            f"📈 **Engagement**\n"
            f"   • Avg quizzes per group: {total_quizzes_sent/total_groups if total_groups > 0 else 0:.1f}\n"
            f"   • Total engagement score: {sum(self.stats['group_engagement'].values())}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("📋 Export Data", callback_data="export_data")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(stats_text, reply_markup=reply_markup)
    
    async def start_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start broadcast mode"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        self.broadcast_mode[user_id] = True
        
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = (
            f"📢 **Broadcast Mode Activated**\n\n"
            f"Please send the message you want to broadcast to all {len(self.groups)} groups.\n\n"
            f"⚠️ **Warning:** This will send your message to all groups immediately!\n"
            f"✏️ Type your message now..."
        )
        
        if update.callback_query:
            await update.callback_query.edit_message_text(message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message, reply_markup=reply_markup)
    
    async def send_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
        """Send broadcast message to all groups"""
        user_id = update.effective_user.id
        self.broadcast_mode[user_id] = False
        
        sent_to = 0
        failed_groups = []
        
        # Send to all groups
        for group in self.groups:
            try:
                await self.application.bot.send_message(
                    chat_id=group['chat_id'],
                    text=f"📢 **Announcement**\n\n{message_text}\n\n- Admin"
                )
                sent_to += 1
                await asyncio.sleep(0.5)  # Rate limiting
            except Exception as e:
                failed_groups.append(group['title'])
                print(f"Failed to broadcast to {group['title']}: {e}")
        
        # Send report to admin
        report = (
            f"✅ **Broadcast Completed**\n\n"
            f"📤 Sent to: {sent_to}/{len(self.groups)} groups\n"
            f"✅ Successful: {sent_to}\n"
            f"❌ Failed: {len(failed_groups)}\n"
        )
        
        if failed_groups:
            report += f"\nFailed groups:\n" + "\n".join(failed_groups[:10])
            if len(failed_groups) > 10:
                report += f"\n... and {len(failed_groups) - 10} more"
        
        await update.message.reply_text(report)
    
    async def export_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Export bot data to JSON and CSV files"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        try:
            # Export quizzes to CSV
            if self.quizzes:
                with open('quizzes_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['id', 'type', 'question', 'options', 'is_anonymous', 'allows_multiple_answers', 'correct_option_id', 'added_date', 'sent_count', 'last_sent']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    for quiz in self.quizzes:
                        # Convert options list to string for CSV
                        quiz_export = quiz.copy()
                        quiz_export['options'] = ' | '.join(quiz['options'])
                        writer.writerow(quiz_export)
                
                # Send quizzes CSV
                await context.bot.send_document(
                    chat_id=user_id,
                    document=open('quizzes_export.csv', 'rb'),
                    filename='quizzes_export.csv',
                    caption="📝 Quizzes Export (CSV)"
                )
            
            # Export groups to CSV
            if self.groups:
                with open('groups_export.csv', 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['chat_id', 'title', 'added_date', 'member_count', 'quizzes_received', 'last_activity']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    for group in self.groups:
                        writer.writerow(group)
                
                # Send groups CSV
                await context.bot.send_document(
                    chat_id=user_id,
                    document=open('groups_export.csv', 'rb'),
                    filename='groups_export.csv',
                    caption="👥 Groups Export (CSV)"
                )
            
            # Export stats to JSON
            with open('stats_export.json', 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=2, ensure_ascii=False)
            
            # Send stats JSON
            await context.bot.send_document(
                chat_id=user_id,
                document=open('stats_export.json', 'rb'),
                filename='stats_export.json',
                caption="📊 Statistics Export (JSON)"
            )
            
            # Send summary
            summary = (
                f"✅ **Data Export Completed**\n\n"
                f"📁 Files exported:\n"
                f"• quizzes_export.csv ({len(self.quizzes)} quizzes)\n"
                f"• groups_export.csv ({len(self.groups)} groups)\n"
                f"• stats_export.json (statistics)\n\n"
                f"💾 All data has been exported successfully!"
            )
            
            if update.callback_query:
                await update.callback_query.edit_message_text(summary)
            else:
                await update.message.reply_text(summary)
                
        except Exception as e:
            error_msg = f"❌ Error exporting data: {str(e)}"
            if update.callback_query:
                await update.callback_query.edit_message_text(error_msg)
            else:
                await update.message.reply_text(error_msg)
    
    async def manage_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show group management interface"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        total_groups = len(self.groups)
        active_groups = len([g for g in self.groups if g['quizzes_received'] > 0])
        
        groups_text = (
            f"👥 **Group Management**\n\n"
            f"📊 **Overview**\n"
            f"• Total groups: {total_groups}\n"
            f"• Active groups: {active_groups}\n"
            f"• Inactive groups: {total_groups - active_groups}\n\n"
        )
        
        # Show top 5 most active groups
        sorted_groups = sorted(self.groups, key=lambda x: x['quizzes_received'], reverse=True)[:5]
        
        if sorted_groups:
            groups_text += "🏆 **Top 5 Active Groups:**\n"
            for i, group in enumerate(sorted_groups, 1):
                groups_text += f"{i}. {group['title']} - {group['quizzes_received']} quizzes\n"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="manage_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(groups_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(groups_text, reply_markup=reply_markup)
    
    async def clean_inactive_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove inactive groups"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Find groups that haven't received any quizzes (likely removed the bot)
        inactive_groups = [g for g in self.groups if g['quizzes_received'] == 0]
        
        if not inactive_groups:
            await update.callback_query.answer("No inactive groups found!")
            return
        
        # Remove inactive groups
        self.groups = [g for g in self.groups if g['quizzes_received'] > 0]
        self.save_data(GROUPS_FILE, self.groups)
        
        await update.callback_query.edit_message_text(
            f"✅ **Cleaned {len(inactive_groups)} inactive groups**\n\n"
            f"Removed groups that never received any quizzes (likely removed the bot).\n"
            f"Current active groups: {len(self.groups)}"
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "stats":
            await self.show_stats(update, context)
        elif data == "add_quiz":
            await query.edit_message_text(
                "📝 **Add New Quiz Poll**\n\n"
                "To add a quiz:\n\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. (Optional) Enable 'Quiz Mode' for correct answers\n"
                "5. Send the poll to me\n\n"
                "I'll automatically save it and send it to groups every hour!\n\n"
                "💡 **Tip:** Use Quiz Mode for questions with right/wrong answers!"
            )
        elif data == "broadcast":
            await self.start_broadcast(update, context)
        elif data == "manage_groups":
            await self.manage_groups(update, context)
        elif data == "export_data":
            await self.export_data(update, context)
        elif data == "cancel_broadcast":
            user_id = query.from_user.id
            self.broadcast_mode[user_id] = False
            await query.edit_message_text("❌ Broadcast cancelled.")
        elif data == "clean_inactive":
            await self.clean_inactive_groups(update, context)
        elif data.startswith("remove_group_"):
            chat_id = int(data.split("_")[2])
            await self.remove_group(update, context, chat_id)
        elif data.startswith("group_stats_"):
            chat_id = int(data.split("_")[2])
            await self.show_group_stats(update, context, chat_id)
    
    async def remove_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Remove a group from the list"""
        self.groups = [g for g in self.groups if g['chat_id'] != chat_id]
        self.save_data(GROUPS_FILE, self.groups)
        
        await update.callback_query.edit_message_text(
            f"✅ Group removed from database.\n\n"
            f"The bot will stop sending quizzes to this group."
        )
    
    async def show_group_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Show statistics for a specific group"""
        group = next((g for g in self.groups if g['chat_id'] == chat_id), None)
        
        if not group:
            await update.callback_query.answer("Group not found!")
            return
        
        stats_text = (
            f"📊 **Group Statistics**\n\n"
            f"🏷️ **Name:** {group['title']}\n"
            f"🆔 **ID:** {group['chat_id']}\n"
            f"📅 **Added:** {datetime.fromisoformat(group['added_date']).strftime('%Y-%m-%d')}\n"
            f"📤 **Quizzes Received:** {group['quizzes_received']}\n"
            f"👥 **Members:** {group.get('member_count', 'Unknown')}\n"
            f"🕐 **Last Activity:** {datetime.fromisoformat(group['last_activity']).strftime('%Y-%m-%d %H:%M')}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("🚫 Remove Group", callback_data=f"remove_group_{chat_id}")],
            [InlineKeyboardButton("👥 All Groups", callback_data="manage_groups")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
    
    def setup_handlers(self):
        """Setup bot handlers"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("broadcast", self.start_broadcast))
        self.application.add_handler(CommandHandler("export", self.export_data))
        self.application.add_handler(CommandHandler("groups", self.manage_groups))
        
        # Handle both text messages and polls
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.POLL) & ~filters.COMMAND, 
            self.handle_private_message
        ))
        
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
    
    async def start_scheduler(self):
        """Start the quiz scheduler"""
        while True:
            await asyncio.sleep(3600)  # Wait 1 hour
            await self.send_random_quiz()
    
    async def run_bot(self):
        """Run the bot"""
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        
        # Start the scheduler
        asyncio.create_task(self.start_scheduler())
        
        print("🤖 Bot is starting...")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        print("✅ Bot is now running with poll quiz support!")
        
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)

def run_flask():
    """Run Flask app"""
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return "Quiz Poll Bot is running!"
    
    @app.route('/health')
    def health():
        return "OK", 200
    
    print(f"🌐 Flask server starting on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def run_bot():
    """Run the bot in its own thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    global bot_instance
    bot_instance = QuizBot()
    
    try:
        loop.run_until_complete(bot_instance.run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Bot error: {e}")
    finally:
        loop.close()

def main():
    """Main function to start both services"""
    # Start Flask in main thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start bot in current thread (this will block)
    run_bot()

if __name__ == '__main__':
    main()