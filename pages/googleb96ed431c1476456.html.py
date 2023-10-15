import streamlit as st
import streamlit.components.v1 as components
plot_file = open('pages\googleb96ed431c1476456.html','r')

plot = plot_file.read()

components.html(plot ,width=200, height=200)

plot = plot_file.close()