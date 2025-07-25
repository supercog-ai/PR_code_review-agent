import { useCallback, useEffect, useRef, useState } from 'react';
import { mutate } from 'swr';

import { useThreadLogs } from '@/hooks/useAgentData';
import { AgentEventType, agenticApi } from '@/lib/api';
import { convertFromUTC, isUserTurn } from '@/lib/utils';

/**
 * Custom hook for handling agent prompt submission and event streaming
 */
export function useChat(agentPath: string, agentName: string, currentThreadId: string | undefined) {
  const [isSending, setIsSending] = useState(false);
  const [events, setEvents] = useState<Ui.Event[]>([]);
  const streamContentRef = useRef<string>('');
  const cleanupRef = useRef<(() => void) | null>(null);
  // Track the last event type to detect non-continuous chat outputs
  const lastEventTypeRef = useRef<string | null>(null);

  // Track the previous threadId to detect changes
  const prevThreadIdRef = useRef<string | undefined | null>(currentThreadId);
  
  // Fetch thread logs when threadId changes
  const { data: threadLogs, isLoading: isLoadingThreadLogs } = useThreadLogs(agentPath, currentThreadId ?? null);
  
  // Reset events when currentThreadId changes to undefined/null
  useEffect(() => {
    // If currentThreadId changed from a value to undefined/null, reset events
    if (prevThreadIdRef.current && !currentThreadId) {
      setEvents([]);
    }
    // Update the ref for next comparison
    prevThreadIdRef.current = currentThreadId;
  }, [currentThreadId]);
  
  // Convert thread logs to Ui.Event format when they change
  useEffect(() => {
    if (threadLogs && threadLogs.length > 0) {
      const processedEvents: Ui.Event[] = [];
      
      // First convert all logs to Ui.Event format
      const eventsFromLogs: Ui.Event[] = threadLogs.map(log => ({
        type: log.event_name,
        payload: log.event.content || log.event.payload || log.event,
        agentName: log.agent_id,
        timestamp: convertFromUTC(log.created_at),
      }));
      
      // Then combine consecutive chat_output events from the same agent
      for (let i = 0; i < eventsFromLogs.length; i++) {
        const event = eventsFromLogs[i];
        
        // If this is a chat_output and the previous event was also a chat_output from the same agent
        if (
          event.type === AgentEventType.CHAT_OUTPUT && 
          processedEvents.length > 0 &&
          processedEvents[processedEvents.length - 1].type === AgentEventType.CHAT_OUTPUT &&
          processedEvents[processedEvents.length - 1].agentName === event.agentName
        ) {
          // Get the previous event
          const prevEvent = processedEvents[processedEvents.length - 1];
          
          // Combine the content
          const prevContent = typeof prevEvent.payload === 'string'
            ? prevEvent.payload 
            : prevEvent.payload?.content || '';
            
          const newContent = typeof event.payload === 'string'
            ? event.payload
            : event.payload?.content || '';
          
          // Update the previous event with combined content
          if (typeof prevEvent.payload === 'string') {
            prevEvent.payload = prevContent + newContent;
          } else {
            prevEvent.payload = {
              ...prevEvent.payload,
              content: prevContent + newContent
            };
          }
          
          // Update the timestamp to the latest one
          prevEvent.timestamp = event.timestamp;
        } else {
          // Add as a new event
          processedEvents.push(event);
        }
      }
      
      setEvents(processedEvents);
    } else if (threadLogs && threadLogs.length === 0) {
      // Reset events when we get empty logs
      setEvents([]);
    }
  }, [threadLogs]);
  
  // Function to clean up any active stream
  const cleanupStream = useCallback(() => {
    if (cleanupRef.current) {
      cleanupRef.current();
      cleanupRef.current = null;
    }
  }, []);

  // Process the event stream from the agent
  const processEventStream = useCallback(async (
    requestId: string,
    threadId: string,
    onStreamContent: (_content: string) => void,
    isBackground: boolean,
    onComplete?: () => void
  ) => {
    // Clean up any existing stream first
    cleanupStream();
    
    return new Promise<void>((resolve, reject) => {
      try {
        // Set up the event stream
        const cleanup = agenticApi.streamEvents(
          agentPath, 
          agentName, 
          requestId, 
          (event: Api.AgentEvent) => {
            // Create a UI event from the API event
            const uiEvent: Ui.Event = {
              type: event.type,
              payload: event.payload,
              agentName: event.agent,
              timestamp: new Date(),
              isBackground: isBackground
            };
            
            // Handle chat output events
            if (event.type === AgentEventType.CHAT_OUTPUT) {
              // Reset streamContentRef if this is a new chat_output not preceded by another chat_output
              if (lastEventTypeRef.current !== AgentEventType.CHAT_OUTPUT) {
                streamContentRef.current = '';
              }
              
              const content = event.payload.content || '';
              streamContentRef.current += content;
              onStreamContent(content);
              
              // Update the events state
              setEvents(prev => {
                // Check if the previous event is a chat output from the same agent
                const previousEventIndex = prev.length - 1;
                const isPreviousEventChatOutput = 
                  previousEventIndex >= 0 && 
                  prev[previousEventIndex].type === AgentEventType.CHAT_OUTPUT && 
                  prev[previousEventIndex].agentName === event.agent;

                
                if (isPreviousEventChatOutput) {
                  // Update existing event
                  const updatedEvents = [...prev];
                  updatedEvents[previousEventIndex] = {
                    ...updatedEvents[previousEventIndex],
                    payload: {
                      ...updatedEvents[previousEventIndex].payload,
                      content: streamContentRef.current
                    }
                  };
                  return updatedEvents;
                } else {
                  // Add new event
                  return [...prev, uiEvent];
                }
              });
            } 
            // Add non-chat output events to the events list
            // Special case: always add reasoning content events, even in background mode
            else if (!isBackground || uiEvent.type === AgentEventType.REASONING_CONTENT) {
              setEvents(prev => [...prev, uiEvent]);
            }

            // Update the lastEventType reference
            lastEventTypeRef.current = event.type;
            
            // Handle turn end
            if (isUserTurn(agentName, event)) {
              cleanup();
              if (onComplete) onComplete();
              resolve();
            }
          }
        );
        
        // Store the cleanup function
        cleanupRef.current = cleanup;
      } catch (error) {
        console.error('Error processing event stream:', error);
        reject(error);
      }
    });
  }, [agentPath, agentName, cleanupStream]);

  // Send a prompt to the agent (foreground mode)
  const sendPrompt = useCallback(async (
    promptText: string, 
    existingThreadId?: string,
    onMessageUpdate?: (_content: string) => void,
    onComplete?: (_threadId: string) => void
  ) => {
    if (!promptText.trim()) return null;
    
    try {
      setIsSending(true);
      streamContentRef.current = '';
      lastEventTypeRef.current = null; // Reset the last event type
      
      // Send the prompt to the agent
      const response = await agenticApi.sendPrompt(agentPath, promptText, existingThreadId);
      const requestId = response.request_id;
      const threadId = response.thread_id;

      // Set up event streaming
      await processEventStream(
        requestId, 
        threadId, 
        () => {
          onMessageUpdate?.(streamContentRef.current);
        },
        false
      );

      // Refresh threads data when complete
      if (onComplete) {
        onComplete(threadId);
        mutate(['agent-threads', agentPath]);
      }

      return {
        requestId,
        threadId,
        content: streamContentRef.current
      };
    } catch (error) {
      console.error('Error sending prompt:', error);
      return null;
    } finally {
      setIsSending(false);
    }
  }, [agentPath, processEventStream]);

  // Send a prompt in background mode
  const sendBackgroundPrompt = useCallback(async (
    promptText: string,
    existingThreadId?: string,
    onMessageUpdate?: (_requestId: string, _content: string) => void,
    onComplete?: (_requestId: string) => void
  ) => {
    if (!promptText.trim()) return null;
    
    try {
      // Reset the last event type reference
      lastEventTypeRef.current = null;
      
      // Send the prompt to the agent
      const response = await agenticApi.sendPrompt(agentPath, promptText, existingThreadId);
      const requestId = response.request_id;
      const threadId = response.thread_id;
      
      let contentAccumulator = '';
      
      // Process the stream in the background
      processEventStream(
        requestId,
        threadId,
        (newContent) => {
          contentAccumulator += newContent;
          onMessageUpdate?.(requestId, contentAccumulator);
        },
        true,
        () => {
          onComplete?.(requestId);
          mutate(['agent-threads', agentPath]);
        }
      );

      return {
        requestId,
        threadId
      };
    } catch (error) {
      console.error('Error sending background prompt:', error);
      return null;
    }
  }, [agentPath, processEventStream]);

  const resumeWithInput = useCallback(async (
    continueResult: Record<string, string>, 
    existingThreadId: string,
    onMessageUpdate?: (_content: string) => void,
    onComplete?: (_threadId: string) => void
  ) => {
    if (Object.keys(continueResult).length === 0) return null;
    
    try {
      setIsSending(true);
      streamContentRef.current = '';
      lastEventTypeRef.current = null; // Reset the last event type
      
      // Send the prompt to the agent
      const response = await agenticApi.resumeWithInput(agentPath, continueResult, existingThreadId);
      const requestId = response.request_id;
      const threadId = response.thread_id;

      // Set up event streaming
      await processEventStream(
        requestId, 
        threadId, 
        () => {
          onMessageUpdate?.(streamContentRef.current);
        },
        false
      );

      // Refresh threads data when complete
      if (onComplete) {
        onComplete(threadId);
        mutate(['agent-threads', agentPath]);
      }

      return {
        requestId,
        threadId,
        content: streamContentRef.current
      };
    } catch (error) {
      console.error('Error resuming chat:', error);
      return null;
    } finally {
      setIsSending(false);
    }
  }, [agentPath, processEventStream]);

  // Cancel any ongoing stream when component unmounts
  const cancelStream = useCallback(() => {
    cleanupStream();
  }, [cleanupStream]);
  
  // Helper function to find reasoning content for a chat output event
  const findReasoningForChatOutput = (chatOutputIndex: number, allEvents: Ui.Event[]): string | undefined => {
    // First, look backwards from the chat output event (reasoning may come before chat output)
    for (let i = chatOutputIndex - 1; i >= 0; i--) {
      const event = allEvents[i];
      if (event.type === AgentEventType.REASONING_CONTENT && event.agentName === agentName) {
        return typeof event.payload === 'string' 
          ? event.payload 
          : event.payload?.reasoning_content || event.payload?.content || '';
      }
      // Stop looking backwards if we hit another chat output or user message
      if (event.type === AgentEventType.CHAT_OUTPUT || 
          event.type === AgentEventType.PROMPT_STARTED || 
          event.type === AgentEventType.RESUME_WITH_INPUT) {
        break;
      }
    }
    
    // If not found backwards, look forwards from the chat output event (reasoning may come after chat output)
    for (let i = chatOutputIndex + 1; i < allEvents.length; i++) {
      const event = allEvents[i];
      if (event.type === AgentEventType.REASONING_CONTENT && event.agentName === agentName) {
        return typeof event.payload === 'string' 
          ? event.payload 
          : event.payload?.reasoning_content || event.payload?.content || '';
      }
      // Stop looking forwards if we hit another chat output or user message
      if (event.type === AgentEventType.CHAT_OUTPUT || 
          event.type === AgentEventType.PROMPT_STARTED || 
          event.type === AgentEventType.RESUME_WITH_INPUT ||
          event.type === AgentEventType.TURN_END) {
        break;
      }
    }
    
    return undefined;
  };

  // Derive messages from events for chat display
  const messages = events
    .filter(event => (
      !event.isBackground &&
      (event.type === AgentEventType.PROMPT_STARTED ||
       event.type === AgentEventType.RESUME_WITH_INPUT ||
       event.type === AgentEventType.CHAT_OUTPUT || 
       event.type === AgentEventType.WAIT_FOR_INPUT) &&
      event.agentName === agentName
    ))
    .map((event, index, filteredEvents) => {
      if (event.type === AgentEventType.PROMPT_STARTED || event.type === AgentEventType.RESUME_WITH_INPUT) {
        // Check if the previous message was a WAIT_FOR_INPUT
        const prevEvent = index > 0 ? filteredEvents[index - 1] : null;
        const isFormSubmission = prevEvent?.type === AgentEventType.WAIT_FOR_INPUT;
        
        // TODO: Maybe don't show this since it is already in the form
        let content = typeof event.payload === 'string' ? event.payload : event.payload?.content || '';
        if (isFormSubmission) {
          if (typeof event.payload === 'string') {
            content = event.payload;
          } else if (typeof event.payload === 'object') {
            const values = event.payload.content 
              ? JSON.parse(event.payload.content)
              : event.payload;
            content = Object.values(values).join('\n');
          }
        }
        
        
        return {
          role: 'user' as const,
          content,
          reasoning: undefined
        };
      } else if (event.type === AgentEventType.WAIT_FOR_INPUT) {
        // Check if there's a PROMPT_STARTED event after this WAIT_FOR_INPUT event
        // This would contain the user's form submission
        // This is built around deep_researcher. It may need an OR resumeWithInput according to how the next_turn in deep_research is handled. 
        const promptStartedIndex = filteredEvents.findIndex((e, i) => 
          i > index && 
          (
            e.type === AgentEventType.PROMPT_STARTED ||
            e.type === AgentEventType.RESUME_WITH_INPUT
          ) && 
          e.agentName === event.agentName
        );
        
        const hasSubmission = promptStartedIndex !== -1;
        const submissionEvent = hasSubmission ? filteredEvents[promptStartedIndex] : null;
        const submissionValues = submissionEvent?.payload;
        
        return {
          role: 'agent' as const,
          inputKeys: event.payload,
          resumeValues: hasSubmission ? submissionValues : undefined,
          formDisabled: hasSubmission,
          reasoning: undefined
        };
      } else {
        // This is a CHAT_OUTPUT event, check for reasoning content
        const eventIndexInAllEvents = events.findIndex(e => e === event);
        const reasoning = findReasoningForChatOutput(eventIndexInAllEvents, events);
        
        return {
          role: 'agent' as const,
          content: typeof event.payload === 'string'
            ? event.payload
            : event.payload?.content || '',
          reasoning
        };
      }
    });

  // Add a agent message to the end if the last message is from the user. This allows use to show a loading state.
  if (
      messages.length > 0 &&
      messages[messages.length - 1].role === 'user' &&
      !(events[events.length - 1]?.type === AgentEventType.TURN_END && events[events.length - 1]?.agentName === agentName)
    ) {
    messages.push({
      role: 'agent' as const,
      content: '',
      reasoning: undefined
    });
  }

  return {
    sendPrompt,
    sendBackgroundPrompt,
    resumeWithInput,
    cancelStream,
    events,
    messages,
    isSending,
    isLoadingThreadLogs
  };
}
