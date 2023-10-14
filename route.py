import streamlit as st
import extra_streamlit_components as stx
from web import main


def show_router_controls():
    @st.cache_resource(hash_funcs={"_thread.RLock": lambda _: None})
    def init_router():
        return stx.Router({"/": main, "/landing": landing})

    def landing():
        return st.write("This is the landing page")
    
if __name__ == "__main__":
    show_router_controls()