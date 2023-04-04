import asyncio
import logging
import threading

import discord
from discord import Message as DiscordMessage
from discord.ext import commands
from flask import Flask

from src import completion
from src.base import Message
from src.completion import generate_completion_response, process_response
from src.constants import (
  BOT_INVITE_URL,
  DISCORD_BOT_TOKEN,
  ACTIVATE_THREAD_PREFIX,
  MAX_THREAD_MESSAGES,
  SECONDS_DELAY_RECEIVING_MSG,
)
from src.moderation import (
  moderate_message,
  send_moderation_blocked_message,
  send_moderation_flagged_message,
)
from src.utils import (
  logger,
  should_block,
  close_thread,
  is_last_message_stale,
  discord_message_to_message,
  split_into_shorter_messages,
)

app = Flask(__name__)


@app.route('/')
def main():
  return 'ChatGPT'


logging.basicConfig(
  format="[%(asctime)s] [%(filename)s:%(lineno)d] %(message)s",
  level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


@client.event
async def on_ready():
  logger.info(
    f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
  completion.MY_BOT_NAME = client.user.name
  await tree.sync()


def is_me(interaction):
  return interaction.user.id == "879984855830659073"


@tree.command(name="delete", description="Delete a message")
@commands.check(is_me)
async def delete_command(interaction: discord.Interaction, message_id: str):
  message = await interaction.channel.fetch_message(message_id)
  await message.delete()
  await interaction.response.send_message(
    embed=discord.Embed(
      description="Message deleted",
      color=discord.Color.red(),
    ),
    ephemeral=True,
  )


# /ask message:
@tree.command(name="ask", description="Replies a single message")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
async def ask_command(interaction: discord.Interaction, message: str):
  try:
    # only support creating thread in text channel
    if not isinstance(interaction.channel, discord.TextChannel):
      return

    # block servers not in allow list
    if should_block(guild=interaction.guild):
      return

    user = interaction.user
    logger.debug(f"Chat command by {user} {message[:20]}")

    await interaction.response.defer(thinking=True)

    # fetch completion
    messages = [Message(user=user.name, text=message)]
    response_data = await generate_completion_response(messages=messages,
                                                       user=user)

    # attach the question
    reply_message = f"> {message}\n\n" + response_data.reply_text

    # send the result
    shorter_response = split_into_shorter_messages(reply_message)
    for r in shorter_response:
      await interaction.followup.send(r)

  except Exception as e:
    logger.exception(e)
    await interaction.response.send_message(f"Failed to reply. {str(e)}",
                                            ephemeral=True)


# /chat message:
@tree.command(name="chat", description="Create a new thread for conversation")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
async def chat_command(interaction: discord.Interaction, message: str):
  try:
    # only support creating thread in text channel
    if not isinstance(interaction.channel, discord.TextChannel):
      return

    # block servers not in allow list
    if should_block(guild=interaction.guild):
      return

    user = interaction.user
    logger.debug(f"Chat command by {user} {message[:20]}")
    try:
      # moderate the message
      flagged_str, blocked_str = moderate_message(message=message,
                                                  user=user.name)
      await send_moderation_blocked_message(
        guild=interaction.guild,
        user=user.name,
        blocked_str=blocked_str,
        message=message,
      )
      if len(blocked_str) > 0:
        # message was blocked
        await interaction.response.send_message(
          f"Your prompt has been blocked by moderation.\n{message}",
          ephemeral=True,
        )
        return

      embed = discord.Embed(
        description=f"<@{user.id}> started a chat.",
        color=discord.Color.green(),
      )
      embed.add_field(name=user.name, value=message)

      if len(flagged_str) > 0:
        # message was flagged
        embed.colour = discord.Color.yellow()
        embed.title = "⚠️ This prompt was flagged by moderation."

      await interaction.response.send_message(embed=embed)
      response = await interaction.original_response()

      await send_moderation_flagged_message(
        guild=interaction.guild,
        user=user.name,
        flagged_str=flagged_str,
        message=message,
        url=response.jump_url,
      )
    except Exception as e:
      logger.exception(e)
      await interaction.response.send_message(f"Failed to start chat {str(e)}",
                                              ephemeral=True)
      return

    # create the thread
    thread = await response.create_thread(
      name=f"{ACTIVATE_THREAD_PREFIX} {user.name[:20]} - {message[:30]}",
      reason="ChatGPT",
    )
    async with thread.typing():
      # fetch completion
      messages = [Message(user=user.name, text=message)]
      response_data = await generate_completion_response(messages=messages,
                                                         user=user)
      # send the result
      await process_response(user=user.name,
                             thread=thread,
                             response_data=response_data)
  except Exception as e:
    logger.exception(e)
    await interaction.response.send_message(f"Failed to start chat {str(e)}",
                                            ephemeral=True)


# calls for each message
@client.event
async def on_message(message: DiscordMessage):
  try:
    # block servers not in allow list
    if should_block(guild=message.guild):
      return

    # ignore messages from the bot
    if message.author == client.user:
      return

    # ignore messages not in a thread
    channel = message.channel
    if not isinstance(channel, discord.Thread):
      return

    # ignore threads not created by the bot
    thread = channel
    if thread.owner_id != client.user.id:
      return

    # ignore threads that are archived locked or title is not what we want
    if (thread.archived or thread.locked
        or not thread.name.startswith(ACTIVATE_THREAD_PREFIX)):
      # ignore this thread
      return

    if thread.message_count > MAX_THREAD_MESSAGES:
      # too many messages, no longer going to reply
      await close_thread(thread=thread)
      return

    # moderate the message
    flagged_str, blocked_str = moderate_message(message=message.content,
                                                user=message.author.name)

    if len(blocked_str) > 0:
      try:
        await message.delete()
        await thread.send(embed=discord.Embed(
          description=
          f"❌ **{message.author}'s message has been deleted by moderation.**",
          color=discord.Color.red(),
        ))
        return
      except PermissionError:
        await thread.send(embed=discord.Embed(
          description=
          f"❌ **{message.author}'s message has been blocked by moderation but could not be "
          f"deleted. Missing Manage Messages permission in this Channel.**",
          color=discord.Color.red(),
        ))
        return

    if len(flagged_str) > 0:
      await thread.send(embed=discord.Embed(
        description=
        f"⚠️ **{message.author}'s message has been flagged by moderation.**",
        color=discord.Color.yellow(),
      ))

    # wait a bit in case user has more messages
    if SECONDS_DELAY_RECEIVING_MSG > 0:
      await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)
      if is_last_message_stale(
          interaction_message=message,
          last_message=thread.last_message,
          bot_id=str(client.user.id),
      ):
        # there is another message, so ignore this one
        return

    logger.info(
      f"Thread message to process - {message.author}: {message.content[:50]} - {thread.name} {thread.jump_url}"
    )

    if is_last_message_stale(
        interaction_message=message,
        last_message=thread.last_message,
        bot_id=str(client.user.id),
    ):
      # there is another message and its not from us, so ignore this response
      return

    channel_messages = [
      discord_message_to_message(message)
      async for message in thread.history(limit=MAX_THREAD_MESSAGES)
    ]
    channel_messages = [x for x in channel_messages if x is not None]
    channel_messages.reverse()

    # generate the response
    async with thread.typing():
      response_data = await generate_completion_response(
        messages=channel_messages, user=message.author)
      if is_last_message_stale(
          interaction_message=message,
          last_message=thread.last_message,
          bot_id=str(client.user.id),
      ):
        # there is another message and its not from us, so ignore this response
        return
      print(message.channel)
      await process_response(message.author.name, thread, response_data)

  except Exception as e:
    logger.exception(e)


def start_server():
  app.run(host='0.0.0.0', port=1234, debug=False)


threading.Thread(target=start_server).start()
client.run(DISCORD_BOT_TOKEN)
