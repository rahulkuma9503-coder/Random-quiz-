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
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
PORT = int(os.getenv('PORT', 10000))
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/quizbot')

# Global bot instance
bot_instance = None

class MongoDB:
    def __init__(self, uri):
        self.uri = uri
        self.client = None
        self.db = None
        self.connect()
    
    def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = MongoClient(self.uri)
            self.db = self.client.quizbot
            # Test connection
            self.client.admin.command('ping')
            print("✅ Connected to MongoDB successfully!")
        except ConnectionFailure as e:
            print(f"❌ MongoDB connection failed: {e}")
            # Fallback to in-memory storage
            self.db = None
    
    def is_connected(self):
        """Check if MongoDB is connected"""
        return self.db is not None
    
    def get_collection(self, name):
        """Get a collection from MongoDB"""
        if self.db is not None:
            return self.db[name]
        return None
    
    def insert_one(self, collection_name, document):
        """Insert one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.insert_one(document)
        return None
    
    def find(self, collection_name, query=None):
        """Find documents"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return list(collection.find(query or {}))
        return []
    
    def find_one(self, collection_name, query):
        """Find one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.find_one(query)
        return None
    
    def update_one(self, collection_name, query, update):
        """Update one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.update_one(query, update)
        return None
    
    def delete_one(self, collection_name, query):
        """Delete one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.delete_one(query)
        return None
    
    def delete_many(self, collection_name, query=None):
        """Delete multiple documents"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.delete_many(query or {})
        return None
    
    def replace_one(self, collection_name, query, replacement):
        """Replace one document"""
        collection = self.get_collection(collection_name)
        if collection is not None:
            return collection.replace_one(query, replacement)
        return None

class QuizBot:
    def __init__(self):
        self.application = None
        self.mongo = MongoDB(MONGODB_URI)
        self.quizzes = self.load_quizzes()
        self.groups = self.load_groups()
        self.settings = self.load_settings()
        self.stats = self.load_stats()
        self.broadcast_mode = {}
        self.scheduler_task = None
        self.quiz_interval = self.settings.get('quiz_interval', 3600)  # Default 1 hour
        
    def load_quizzes(self):
        """Load quizzes from MongoDB"""
        return self.mongo.find('quizzes')
    
    def load_groups(self):
        """Load groups from MongoDB"""
        return self.mongo.find('groups')
    
    def load_settings(self):
        """Load settings from MongoDB"""
        settings = self.mongo.find_one('settings', {'_id': 'bot_settings'})
        if not settings:
            # Default settings
            settings = {
                '_id': 'bot_settings',
                'quiz_interval': 3600,  # 1 hour in seconds
                'max_quizzes_per_day': 24,
                'auto_clean_inactive': True,
                'inactive_days_threshold': 7,
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat()
            }
            self.mongo.insert_one('settings', settings)
        return settings
    
    def load_stats(self):
        """Load stats from MongoDB"""
        stats = self.mongo.find_one('stats', {'_id': 'bot_stats'})
        if not stats:
            # Default stats
            stats = {
                '_id': 'bot_stats',
                'total_quizzes_sent': 0,
                'total_groups_reached': 0,
                'quizzes_added': 0,
                'bot_start_time': datetime.now().isoformat(),
                'last_quiz_sent': None,
                'group_engagement': {},
                'total_broadcasts_sent': 0,
                'manual_quizzes_sent': 0
            }
            self.mongo.insert_one('stats', stats)
        return stats
    
    def save_quiz(self, quiz):
        """Save quiz to MongoDB"""
        if '_id' in quiz:
            self.mongo.replace_one('quizzes', {'_id': quiz['_id']}, quiz)
        else:
            result = self.mongo.insert_one('quizzes', quiz)
            if result and result.inserted_id:
                quiz['_id'] = result.inserted_id
    
    def save_group(self, group):
        """Save group to MongoDB"""
        if '_id' in group:
            self.mongo.replace_one('groups', {'_id': group['_id']}, group)
        else:
            result = self.mongo.insert_one('groups', group)
            if result and result.inserted_id:
                group['_id'] = result.inserted_id
    
    def save_settings(self):
        """Save settings to MongoDB"""
        self.settings['updated_at'] = datetime.now().isoformat()
        self.mongo.replace_one('settings', {'_id': 'bot_settings'}, self.settings)
    
    def save_stats(self):
        """Save stats to MongoDB"""
        self.mongo.replace_one('stats', {'_id': 'bot_stats'}, self.stats)

    async def ensure_group_registered(self, chat_id, chat_title=None):
        """Ensure a group is registered in the database"""
        existing_group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        if not existing_group:
            # Register the group
            group_info = {
                'chat_id': chat_id,
                'title': chat_title or f"Group {chat_id}",
                'added_date': datetime.now().isoformat(),
                'member_count': 0,
                'quizzes_received': 0,
                'manual_quizzes_received': 0,
                'last_activity': datetime.now().isoformat(),
                'is_active': True
            }
            self.mongo.insert_one('groups', group_info)
            self.groups = self.load_groups()  # Reload groups
            print(f"✅ Auto-registered group: {chat_title or chat_id}")
        
        return self.mongo.find_one('groups', {'chat_id': chat_id})
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        chat_type = update.effective_chat.type
        
        if chat_type == 'private':
            if user_id == ADMIN_USER_ID:
                keyboard = [
                    [InlineKeyboardButton("📊 View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("📝 Add Quiz", callback_data="add_quiz")],
                    [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
                    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
                    [InlineKeyboardButton("👥 Manage Groups", callback_data="manage_groups")],
                    [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
                    [InlineKeyboardButton("📋 Export Data", callback_data="export_data")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                quiz_interval_hours = self.quiz_interval / 3600
                
                await update.message.reply_text(
                    f"👋 **Admin Dashboard**\n\n"
                    f"I'm your Quiz Bot! Choose an option below:\n\n"
                    f"📊 **Statistics** - View detailed bot analytics\n"
                    f"📝 **Add Quiz** - Create and send me a QUIZ MODE poll to save\n"
                    f"⚙️ **Settings** - Configure bot settings (Current: {quiz_interval_hours}h interval)\n"
                    f"📢 **Broadcast** - Send message to all groups\n"
                    f"👥 **Manage Groups** - View and manage groups\n"
                    f"🔄 **Reset Quizzes** - Delete all saved quizzes\n"
                    f"📋 **Export Data** - Export quizzes and stats\n\n"
                    f"📢 **Important:** I only accept QUIZ MODE polls (with correct answers)!",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "👋 Hello! I'm a quiz bot that sends random quiz polls regularly.\n\n"
                    "Add me to your group and make me an admin to start receiving fun quiz polls!\n\n"
                    "⚡ **Group Commands:**\n"
                    "• /rquiz - Send immediate random quiz (Group admins only)"
                )
        else:
            # Bot added to a group
            await self.add_to_group(update)
    
    async def add_to_group(self, update: Update):
        """Handle bot being added to a group"""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title
        
        # Check if group already exists in MongoDB
        existing_group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        group_info = {
            'chat_id': chat_id,
            'title': chat_title,
            'added_date': datetime.now().isoformat(),
            'member_count': update.effective_chat.get_member_count() if update.effective_chat.get_member_count else 0,
            'quizzes_received': existing_group['quizzes_received'] if existing_group else 0,
            'manual_quizzes_received': existing_group['manual_quizzes_received'] if existing_group else 0,
            'last_activity': datetime.now().isoformat(),
            'is_active': True
        }
        
        if existing_group:
            # Update existing group
            group_info['_id'] = existing_group['_id']
            self.mongo.replace_one('groups', {'_id': existing_group['_id']}, group_info)
            message = f"🎉 I'm back in {chat_title}! I'll continue sending quiz polls.\n\nUse /rquiz to send an immediate quiz!"
        else:
            # Add new group
            self.mongo.insert_one('groups', group_info)
            message = f"🎉 Thanks for adding me to {chat_title}!\n\nI'll send random quiz polls automatically!\n\nUse /rquiz to send an immediate quiz!"
        
        # Reload groups from MongoDB
        self.groups = self.load_groups()
        
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
                "❌ Please send a QUIZ MODE poll to save as a quiz!\n\n"
                "To create a QUIZ MODE poll:\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. ✅ Enable 'Quiz Mode' and set correct answer\n"
                "5. Send it to me\n\n"
                "🚫 I only accept QUIZ MODE polls (with correct answers)!\n"
                "🚫 Regular polls will be ignored."
            )
    
    async def save_poll_quiz(self, update: Update, poll):
        """Save a poll as a quiz - ONLY if it's a QUIZ MODE poll"""
        # Check if it's a quiz mode poll (has correct answer)
        if poll.type != Poll.QUIZ:
            await update.message.reply_text(
                "❌ **Regular Poll Ignored!**\n\n"
                "I only accept **QUIZ MODE** polls with correct answers.\n\n"
                "📝 **To create a QUIZ MODE poll:**\n"
                "1. Create a poll as usual\n"
                "2. ✅ Enable **'Quiz Mode'** \n"
                "3. Set the correct answer\n"
                "4. Send it to me\n\n"
                "🚫 Regular polls without correct answers are ignored."
            )
            return
        
        # It's a quiz mode poll - save it
        quiz = {
            'type': 'poll',
            'question': poll.question,
            'options': [option.text for option in poll.options],
            'is_anonymous': False,  # Force non-anonymous voting
            'allows_multiple_answers': poll.allows_multiple_answers,
            'correct_option_id': poll.correct_option_id,
            'added_date': datetime.now().isoformat(),
            'sent_count': 0,
            'manual_sent_count': 0,
            'last_sent': None,
            'engagement': 0,
            'is_active': True
        }
        
        self.mongo.insert_one('quizzes', quiz)
        self.stats['quizzes_added'] += 1
        self.save_stats()
        
        # Reload quizzes from MongoDB
        self.quizzes = self.load_quizzes()
        
        # Format options for display
        options_text = "\n".join([f"• {option}" for option in quiz['options']])
        correct_answer = quiz['options'][quiz['correct_option_id']]
        
        await update.message.reply_text(
            f"✅ **Quiz Mode Poll Saved Successfully!**\n\n"
            f"📝 **Question:** {quiz['question']}\n\n"
            f"📋 **Options:**\n{options_text}\n\n"
            f"✅ **Correct Answer:** {correct_answer}\n"
            f"👤 **Voting:** Non-anonymous (voters visible)\n"
            f"📊 Total quizzes: {len(self.quizzes)}\n"
            f"👥 Will be sent to: {len(self.groups)} groups\n"
            f"⏰ Next quiz in: {self.quiz_interval / 3600} hours\n\n"
            f"💡 Group admins can use /rquiz to send immediate quizzes!"
        )
    
    async def send_random_quiz(self):
        """Send a random quiz poll to all groups"""
        if not self.quizzes or not self.groups:
            return
        
        # Get active quizzes only
        active_quizzes = [q for q in self.quizzes if q.get('is_active', True)]
        if not active_quizzes:
            return
        
        quiz = random.choice(active_quizzes)
        
        # Update quiz stats
        quiz['sent_count'] = quiz.get('sent_count', 0) + 1
        quiz['last_sent'] = datetime.now().isoformat()
        self.save_quiz(quiz)
        
        # Update global stats
        self.stats['total_quizzes_sent'] += len(self.groups)
        self.stats['last_quiz_sent'] = datetime.now().isoformat()
        self.save_stats()
        
        sent_to = 0
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        
        for group in active_groups:
            try:
                await self.send_quiz_to_group(group, quiz)
                sent_to += 1
                await asyncio.sleep(0.5)  # Rate limiting
                
            except Exception as e:
                print(f"Failed to send to group {group['chat_id']}: {e}")
                # Mark group as inactive if sending fails repeatedly
                group['is_active'] = False
                self.save_group(group)
        
        # Reload groups and stats after updates
        self.groups = self.load_groups()
        self.save_stats()
        
        print(f"📤 Sent quiz poll to {sent_to}/{len(active_groups)} groups at {datetime.now()}")
    
    async def send_quiz_to_group(self, group, quiz):
        """Send a quiz to a specific group"""
        if quiz['type'] == 'poll':
            # Send as poll with non-anonymous voting
            message = await self.application.bot.send_poll(
                chat_id=group['chat_id'],
                question=f"🎯 Quiz Time: {quiz['question']}",
                options=quiz['options'],
                is_anonymous=False,  # Force non-anonymous voting
                allows_multiple_answers=quiz.get('allows_multiple_answers', False),
                type=Poll.QUIZ,  # Always send as quiz mode
                correct_option_id=quiz.get('correct_option_id'),
                explanation="Check back later for results!",
                open_period=0,  # No time limit
            )
        
        # Update group stats
        group['quizzes_received'] = group.get('quizzes_received', 0) + 1
        group['last_activity'] = datetime.now().isoformat()
        self.save_group(group)
        
        # Track engagement
        if str(group['chat_id']) not in self.stats['group_engagement']:
            self.stats['group_engagement'][str(group['chat_id'])] = 0
        self.stats['group_engagement'][str(group['chat_id'])] += 1
    
    async def send_immediate_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rquiz command - send immediate random quiz to current group"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        chat_title = update.effective_chat.title
        
        # Check if it's a group chat
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command can only be used in groups!")
            return
        
        # Check if user is admin of the group or bot admin
        is_admin = False
        
        # Check if user is bot admin
        if user_id == ADMIN_USER_ID:
            is_admin = True
        else:
            # Check if user is admin in the group
            try:
                chat_member = await context.bot.get_chat_member(chat_id, user_id)
                if chat_member.status in ['administrator', 'creator']:
                    is_admin = True
            except Exception as e:
                print(f"Error checking admin status: {e}")
        
        if not is_admin:
            await update.message.reply_text("❌ Only group admins can use this command!")
            return
        
        # Check if there are active quizzes
        active_quizzes = [q for q in self.quizzes if q.get('is_active', True)]
        if not active_quizzes:
            await update.message.reply_text("❌ No quizzes available! Please add some quiz mode polls first.")
            return
        
        # Ensure group is registered (auto-register if not)
        group = await self.ensure_group_registered(chat_id, chat_title)
        if not group:
            await update.message.reply_text("❌ Failed to register group. Please try again.")
            return
        
        if not group.get('is_active', True):
            # Reactivate the group
            group['is_active'] = True
            self.save_group(group)
        
        # Send typing action
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        
        try:
            # Select random quiz
            quiz = random.choice(active_quizzes)
            
            # Update quiz stats for manual sends
            quiz['manual_sent_count'] = quiz.get('manual_sent_count', 0) + 1
            quiz['last_sent'] = datetime.now().isoformat()
            self.save_quiz(quiz)
            
            # Update group stats for manual quizzes
            group['manual_quizzes_received'] = group.get('manual_quizzes_received', 0) + 1
            group['last_activity'] = datetime.now().isoformat()
            self.save_group(group)
            
            # Update global stats
            self.stats['manual_quizzes_sent'] = self.stats.get('manual_quizzes_sent', 0) + 1
            self.save_stats()
            
            # Send the quiz
            await self.send_quiz_to_group(group, quiz)
            
            # Send confirmation message
            correct_answer = quiz['options'][quiz['correct_option_id']] if quiz.get('correct_option_id') is not None else "Unknown"
            
            await update.message.reply_text(
                f"✅ **Random Quiz Sent!**\n\n"
                f"📝 Question: {quiz['question']}\n"
                f"✅ Correct Answer: {correct_answer}\n"
                f"🕐 Sent by: {update.effective_user.first_name}\n"
                f"📊 This quiz has been sent {quiz.get('sent_count', 0)} times automatically "
                f"and {quiz.get('manual_sent_count', 0)} times manually"
            )
            
        except Exception as e:
            print(f"Error sending immediate quiz: {e}")
            await update.message.reply_text("❌ Failed to send quiz. Please try again later.")
    
    async def reset_quizzes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command - delete all quizzes"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        
        # Create confirmation keyboard
        keyboard = [
            [
                InlineKeyboardButton("✅ YES, Delete All Quizzes", callback_data="confirm_reset"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🚨 **Reset All Quizzes**\n\n"
            "⚠️ **WARNING: This action cannot be undone!**\n\n"
            "This will delete ALL saved quizzes permanently.\n"
            f"📝 **Total quizzes to delete:** {len(self.quizzes)}\n\n"
            "Are you sure you want to continue?",
            reply_markup=reply_markup
        )
    
    async def confirm_reset_quizzes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute quiz reset"""
        user_id = update.callback_query.from_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.callback_query.answer("This command is for admin only.")
            return
        
        # Delete all quizzes from MongoDB
        deleted_count = self.mongo.delete_many('quizzes', {}).deleted_count
        
        # Reset quiz-related stats
        self.stats['quizzes_added'] = 0
        self.stats['total_quizzes_sent'] = 0
        self.stats['manual_quizzes_sent'] = 0
        self.stats['last_quiz_sent'] = None
        self.save_stats()
        
        # Reload quizzes
        self.quizzes = self.load_quizzes()
        
        await update.callback_query.edit_message_text(
            f"✅ **All Quizzes Deleted Successfully!**\n\n"
            f"🗑️ **Deleted quizzes:** {deleted_count}\n"
            f"📊 **Database reset:** All quiz statistics cleared\n\n"
            f"💡 You can now start adding new quiz mode polls!"
        )
    
    async def cancel_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the reset operation"""
        await update.callback_query.edit_message_text(
            "✅ **Reset Cancelled**\n\n"
            "No quizzes were deleted.\n"
            f"📝 Your {len(self.quizzes)} quizzes are safe."
        )
    
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
        manual_quizzes_sent = self.stats.get('manual_quizzes_sent', 0)
        active_groups_count = len([g for g in self.groups if g.get('is_active', True)])
        
        # Calculate active groups (active in last 7 days)
        week_ago = datetime.now() - timedelta(days=7)
        recently_active = len([
            g for g in self.groups 
            if datetime.fromisoformat(g['last_activity']) > week_ago and g.get('is_active', True)
        ])
        
        # Most popular quiz
        most_sent = max(self.quizzes, key=lambda x: x.get('sent_count', 0)) if self.quizzes else None
        
        quiz_interval_hours = self.quiz_interval / 3600
        
        stats_text = (
            f"📊 **Detailed Bot Statistics**\n\n"
            f"📝 **Quizzes Database**\n"
            f"   • Total quizzes: {total_quizzes}\n"
            f"   • Quizzes added: {quizzes_added}\n"
            f"   • Most sent quiz: {most_sent['sent_count'] if most_sent else 0} times\n\n"
            
            f"👥 **Groups Analytics**\n"
            f"   • Total groups: {total_groups}\n"
            f"   • Active groups: {active_groups_count}\n"
            f"   • Recently active: {recently_active}\n"
            f"   • Total quizzes sent: {total_quizzes_sent}\n"
            f"   • Manual quizzes sent: {manual_quizzes_sent}\n\n"
            
            f"⏰ **Performance**\n"
            f"   • Bot started: {datetime.fromisoformat(self.stats['bot_start_time']).strftime('%Y-%m-%d %H:%M')}\n"
            f"   • Last quiz sent: {datetime.fromisoformat(self.stats['last_quiz_sent']).strftime('%Y-%m-%d %H:%M') if self.stats['last_quiz_sent'] else 'Never'}\n"
            f"   • Quiz interval: {quiz_interval_hours} hours\n"
            f"   • Next quiz in: ~{quiz_interval_hours} hours\n\n"
            
            f"📈 **Engagement**\n"
            f"   • Avg quizzes per group: {total_quizzes_sent/total_groups if total_groups > 0 else 0:.1f}\n"
            f"   • Total engagement score: {sum(self.stats['group_engagement'].values())}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
            [InlineKeyboardButton("📋 Export Data", callback_data="export_data")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(stats_text, reply_markup=reply_markup)
    
    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot settings"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        quiz_interval_hours = self.quiz_interval / 3600
        
        settings_text = (
            f"⚙️ **Bot Settings**\n\n"
            f"🕐 **Quiz Interval**: {quiz_interval_hours} hours\n"
            f"   - Current delay between random quizzes\n\n"
            f"📊 **Database**: {'MongoDB' if self.mongo.is_connected() else 'In-Memory'}\n"
            f"   - Data persistence status\n\n"
            f"👥 **Active Groups**: {len([g for g in self.groups if g.get('is_active', True)])}\n"
            f"📝 **Active Quizzes**: {len([q for q in self.quizzes if q.get('is_active', True)])}\n"
            f"🎯 **Manual Quizzes Sent**: {self.stats.get('manual_quizzes_sent', 0)}\n\n"
            f"📢 **Quiz Mode Only**: ✅ Enabled\n"
            f"   - Only accepts polls with correct answers\n\n"
            f"💡 Use /setdelay <hours> to change the quiz interval\n"
            f"💡 Group admins can use /rquiz for immediate quizzes\n"
            f"💡 Use /reset to delete all quizzes"
        )
        
        keyboard = [
            [InlineKeyboardButton("🕐 Set Quiz Interval", callback_data="set_interval")],
            [InlineKeyboardButton("🔄 Reset Quizzes", callback_data="reset_quizzes")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("🔄 Refresh Groups", callback_data="refresh_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(settings_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(settings_text, reply_markup=reply_markup)
    
    async def set_quiz_interval_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setdelay command directly"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Please specify the interval in hours.\n\n"
                "Usage: /setdelay <hours>\n"
                "Example: /setdelay 2 (for 2 hours)\n"
                "Example: /setdelay 0.5 (for 30 minutes)\n\n"
                f"Current interval: {self.quiz_interval / 3600} hours"
            )
            return
        
        try:
            hours = float(context.args[0])
            if hours <= 0:
                await update.message.reply_text("❌ Interval must be greater than 0 hours.")
                return
            
            new_interval = int(hours * 3600)  # Convert to seconds
            old_interval = self.quiz_interval
            
            self.quiz_interval = new_interval
            self.settings['quiz_interval'] = new_interval
            self.save_settings()
            
            await update.message.reply_text(
                f"✅ **Quiz interval updated!**\n\n"
                f"📅 Old interval: {old_interval / 3600} hours\n"
                f"📅 New interval: {hours} hours\n\n"
                f"Next quiz will be sent in approximately {hours} hours."
            )
            
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number (e.g., 2 for 2 hours, 0.5 for 30 minutes)")
    
    async def set_quiz_interval_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set quiz interval from callback (settings menu)"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        await update.callback_query.edit_message_text(
            "🕐 **Set Quiz Interval**\n\n"
            "Please send the new interval in hours.\n\n"
            "Example: `2` for 2 hours, `0.5` for 30 minutes\n\n"
            "Current interval: {} hours".format(self.quiz_interval / 3600)
        )
        
        # Set a flag to expect interval input
        context.user_data['waiting_for_interval'] = True
    
    async def handle_interval_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quiz interval input from settings menu"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID or not context.user_data.get('waiting_for_interval'):
            return
        
        try:
            hours = float(update.message.text)
            if hours <= 0:
                await update.message.reply_text("❌ Interval must be greater than 0 hours.")
                return
            
            new_interval = int(hours * 3600)  # Convert to seconds
            old_interval = self.quiz_interval
            
            self.quiz_interval = new_interval
            self.settings['quiz_interval'] = new_interval
            self.save_settings()
            
            context.user_data['waiting_for_interval'] = False
            
            await update.message.reply_text(
                f"✅ **Quiz interval updated!**\n\n"
                f"📅 Old interval: {old_interval / 3600} hours\n"
                f"📅 New interval: {hours} hours\n\n"
                f"Next quiz will be sent in approximately {hours} hours."
            )
            
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number (e.g., 2 for 2 hours, 0.5 for 30 minutes)")
    
    async def start_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start broadcast mode"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        self.broadcast_mode[user_id] = True
        
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        
        message = (
            f"📢 **Broadcast Mode Activated**\n\n"
            f"Please send the message you want to broadcast to all {active_groups} active groups.\n\n"
            f"⚠️ **Warning:** This will send your message to all active groups immediately!\n"
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
        
        active_groups = [g for g in self.groups if g.get('is_active', True)]
        sent_to = 0
        failed_groups = []
        
        # Send to all active groups
        for group in active_groups:
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
                # Mark group as inactive
                group['is_active'] = False
                self.save_group(group)
        
        # Update stats
        self.stats['total_broadcasts_sent'] = self.stats.get('total_broadcasts_sent', 0) + sent_to
        self.save_stats()
        
        # Reload groups after updates
        self.groups = self.load_groups()
        
        # Send report to admin
        report = (
            f"✅ **Broadcast Completed**\n\n"
            f"📤 Sent to: {sent_to}/{len(active_groups)} active groups\n"
            f"✅ Successful: {sent_to}\n"
            f"❌ Failed: {len(failed_groups)}\n"
        )
        
        if failed_groups:
            report += f"\nFailed groups (marked inactive):\n" + "\n".join(failed_groups[:10])
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
                    fieldnames = ['_id', 'type', 'question', 'options', 'is_anonymous', 'allows_multiple_answers', 'correct_option_id', 'added_date', 'sent_count', 'manual_sent_count', 'last_sent', 'is_active']
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
                    fieldnames = ['_id', 'chat_id', 'title', 'added_date', 'member_count', 'quizzes_received', 'manual_quizzes_received', 'last_activity', 'is_active']
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
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        inactive_groups = total_groups - active_groups
        
        groups_text = (
            f"👥 **Group Management**\n\n"
            f"📊 **Overview**\n"
            f"• Total groups: {total_groups}\n"
            f"• Active groups: {active_groups}\n"
            f"• Inactive groups: {inactive_groups}\n\n"
        )
        
        # Show top 5 most active groups
        active_groups_list = [g for g in self.groups if g.get('is_active', True)]
        sorted_groups = sorted(active_groups_list, key=lambda x: x.get('quizzes_received', 0), reverse=True)[:5]
        
        if sorted_groups:
            groups_text += "🏆 **Top 5 Active Groups:**\n"
            for i, group in enumerate(sorted_groups, 1):
                groups_text += f"{i}. {group['title']} - {group.get('quizzes_received', 0)} auto + {group.get('manual_quizzes_received', 0)} manual quizzes\n"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="manage_groups")],
            [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
            [InlineKeyboardButton("🗑️ Clean Inactive", callback_data="clean_inactive")],
            [InlineKeyboardButton("🔄 Reactivate All", callback_data="reactivate_all")]
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
        
        # Find inactive groups
        inactive_groups = [g for g in self.groups if not g.get('is_active', True)]
        
        if not inactive_groups:
            await update.callback_query.answer("No inactive groups found!")
            return
        
        # Remove inactive groups from MongoDB
        for group in inactive_groups:
            self.mongo.delete_one('groups', {'_id': group['_id']})
        
        # Reload groups
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ **Cleaned {len(inactive_groups)} inactive groups**\n\n"
            f"Removed groups that were marked as inactive (likely removed the bot).\n"
            f"Current active groups: {len([g for g in self.groups if g.get('is_active', True)])}"
        )
    
    async def reactivate_all_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reactivate all groups"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Reactivate all groups
        for group in self.groups:
            group['is_active'] = True
            self.save_group(group)
        
        # Reload groups
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ **All groups reactivated!**\n\n"
            f"All {len(self.groups)} groups have been marked as active and will receive quizzes."
        )
    
    async def refresh_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Refresh groups list"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("This command is for admin only.")
            return
        
        # Reload groups from MongoDB
        self.groups = self.load_groups()
        
        active_groups = len([g for g in self.groups if g.get('is_active', True)])
        
        await update.callback_query.answer(f"Groups refreshed! {active_groups} active groups loaded.")
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "stats":
            await self.show_stats(update, context)
        elif data == "add_quiz":
            await query.edit_message_text(
                "📝 **Add New Quiz Mode Poll**\n\n"
                "To add a quiz:\n\n"
                "1. Click the 📎 attachment icon\n"
                "2. Select 'Poll'\n"
                "3. Enter your question and options\n"
                "4. ✅ **Enable 'Quiz Mode'** (Important!)\n"
                "5. Set the correct answer\n"
                "6. Send it to me\n\n"
                "🚫 **I only accept QUIZ MODE polls with correct answers!**\n"
                "🚫 Regular polls without correct answers will be ignored.\n\n"
                "📢 Make sure to enable **Quiz Mode** when creating the poll!"
            )
        elif data == "settings":
            await self.show_settings(update, context)
        elif data == "broadcast":
            await self.start_broadcast(update, context)
        elif data == "manage_groups":
            await self.manage_groups(update, context)
        elif data == "export_data":
            await self.export_data(update, context)
        elif data == "reset_quizzes":
            await self.reset_quizzes_command(update, context)
        elif data == "confirm_reset":
            await self.confirm_reset_quizzes(update, context)
        elif data == "cancel_reset":
            await self.cancel_reset(update, context)
        elif data == "set_interval":
            await self.set_quiz_interval_callback(update, context)
        elif data == "cancel_broadcast":
            user_id = query.from_user.id
            self.broadcast_mode[user_id] = False
            await query.edit_message_text("❌ Broadcast cancelled.")
        elif data == "clean_inactive":
            await self.clean_inactive_groups(update, context)
        elif data == "reactivate_all":
            await self.reactivate_all_groups(update, context)
        elif data == "refresh_groups":
            await self.refresh_groups(update, context)
        elif data.startswith("remove_group_"):
            chat_id = int(data.split("_")[2])
            await self.remove_group(update, context, chat_id)
        elif data.startswith("group_stats_"):
            chat_id = int(data.split("_")[2])
            await self.show_group_stats(update, context, chat_id)
    
    async def remove_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Remove a group from the list"""
        self.mongo.delete_one('groups', {'chat_id': chat_id})
        self.groups = self.load_groups()
        
        await update.callback_query.edit_message_text(
            f"✅ Group removed from database.\n\n"
            f"The bot will stop sending quizzes to this group."
        )
    
    async def show_group_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Show statistics for a specific group"""
        group = self.mongo.find_one('groups', {'chat_id': chat_id})
        
        if not group:
            await update.callback_query.answer("Group not found!")
            return
        
        status = "🟢 Active" if group.get('is_active', True) else "🔴 Inactive"
        
        stats_text = (
            f"📊 **Group Statistics**\n\n"
            f"🏷️ **Name:** {group['title']}\n"
            f"🆔 **ID:** {group['chat_id']}\n"
            f"📅 **Added:** {datetime.fromisoformat(group['added_date']).strftime('%Y-%m-%d')}\n"
            f"📤 **Auto Quizzes Received:** {group.get('quizzes_received', 0)}\n"
            f"🎯 **Manual Quizzes Received:** {group.get('manual_quizzes_received', 0)}\n"
            f"👥 **Members:** {group.get('member_count', 'Unknown')}\n"
            f"🕐 **Last Activity:** {datetime.fromisoformat(group['last_activity']).strftime('%Y-%m-%d %H:%M')}\n"
            f"📊 **Status:** {status}\n"
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
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("broadcast", self.start_broadcast))
        self.application.add_handler(CommandHandler("export", self.export_data))
        self.application.add_handler(CommandHandler("groups", self.manage_groups))
        self.application.add_handler(CommandHandler("setdelay", self.set_quiz_interval_command))
        self.application.add_handler(CommandHandler("rquiz", self.send_immediate_quiz))
        self.application.add_handler(CommandHandler("reset", self.reset_quizzes_command))
        
        # Handle both text messages and polls
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.POLL) & ~filters.COMMAND, 
            self.handle_private_message
        ))
        
        # Handle interval input from settings menu
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            self.handle_interval_input
        ))
        
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
    
    async def start_scheduler(self):
        """Start the quiz scheduler"""
        while True:
            await asyncio.sleep(self.quiz_interval)  # Use configurable interval
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
        
        quiz_interval_hours = self.quiz_interval / 3600
        print(f"✅ Bot is now running with MongoDB support!")
        print(f"⏰ Quiz interval: {quiz_interval_hours} hours")
        print(f"📊 Loaded {len(self.quizzes)} quizzes and {len(self.groups)} groups from database")
        print(f"🎯 /rquiz command enabled for group admins")
        print(f"🔄 /reset command available for admin")
        print(f"📝 Quiz Mode Only: ✅ Enabled - Only accepts polls with correct answers")
        
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)

def run_flask():
    """Run Flask app"""
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return "Quiz Poll Bot is running with MongoDB!"
    
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